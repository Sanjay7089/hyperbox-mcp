"""Container operations every Runtime implementation needs, and must not
re-derive.

These are the load-bearing parts: the resource-policy read-back, the
network seal, the OOM explanation and garbage collection. Each of them
exists because of a specific failure recorded in the engineering log, and
each is easy to reimplement subtly wrong — the CPU-limit spelling silently
differs between engines, sealing has to fail closed, and GC must never
touch a container it did not create.

They were methods on LLMSandboxRuntime. They are moved here VERBATIM so a
second runtime shares the behaviour rather than a description of it: two
implementations of "seal the network" is two chances to get it wrong, and
only one of them would be covered by the suite that already exists.

The engine is passed in rather than reached for. Nothing here imports a
concrete backend, and nothing here holds a module-level client: callers
supply the backend name and, where a container is needed, a callable that
fetches it. That callable is not decoration — engine.with_retry re-runs the
operation against a rebuilt client when a connection turns out to be stale,
and it can only do that if the container can be looked up again.

This module must never import llm_sandbox. That rule is what keeps the
execution backend replaceable.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from hyperbox_mcp import engine, errors, policy
from hyperbox_mcp.engine import ContainerGoneError, EngineUnavailableError
from hyperbox_mcp.policy import (
    CPU_PERIOD,
    CPU_QUOTA,
    CPUS,
    LABEL_ID,
    LABEL_MANAGED,
    MEM_LIMIT,
    MEM_LIMIT_BYTES,
    NANO_CPUS,
    NO_NEW_PRIVILEGES,
    PIDS_LIMIT,
    TMPFS_PATHS,
    TMPFS_SIZE,
)

#: A container fetched on demand. Re-fetchable on purpose: see the module
#: docstring on with_retry.
GetContainer = Callable[[], Any]


class SandboxRuntimeError(errors.SandboxError, RuntimeError):
    """Wraps any backend exception so callers never see a backend's own
    types — keeps the backend replaceable.

    Still a RuntimeError, so existing handlers are unaffected.
    """

    code = "SANDBOX_FAILED"


# --- what the engine must apply --------------------------------------


def engine_specific(backend: str) -> dict:
    """The same policy, spelled the way this engine's client accepts.

    The two clients diverge in three places and there is no common
    vocabulary:

    - CPU: docker-py takes `nano_cpus`; podman-py DISCARDS it
      silently and honours `cpu_period` / `cpu_quota` instead.
    - Scratch space: docker-py takes a `tmpfs` mapping; podman-py
      rejects that keyword outright and wants tmpfs entries in
      `mounts`.
    - Privilege: `security_opt` versus a `no_new_privileges` flag.

    Getting any of these wrong produces a container that looks
    configured and is not, which is why every one of them is read
    back off the container afterwards.
    """
    if engine.client_dialect(backend) == "podman":
        return {
            "cpu_period": CPU_PERIOD,
            "cpu_quota": CPU_QUOTA,
            "mounts": [
                {
                    "type": "tmpfs",
                    # Without an explicit source podman creates a
                    # plain directory instead of a tmpfs mount.
                    "source": "tmpfs",
                    "target": path,
                    "size": TMPFS_SIZE,
                    "chown": True,
                }
                for path in TMPFS_PATHS
            ],
            "no_new_privileges": NO_NEW_PRIVILEGES,
        }
    return {
        "nano_cpus": NANO_CPUS,
        "tmpfs": {path: f"rw,size={TMPFS_SIZE},mode=1777" for path in TMPFS_PATHS},
        "security_opt": ["no-new-privileges"] if NO_NEW_PRIVILEGES else [],
    }


def runtime_configs(sandbox_id: str, backend: str) -> dict:
    """Everything the engine must apply. Server policy, start to
    finish — no part of this comes from a caller."""
    return {
        "labels": {LABEL_MANAGED: "true", LABEL_ID: sandbox_id},
        "mem_limit": MEM_LIMIT,
        "pids_limit": PIDS_LIMIT,
        **engine_specific(backend),
    }


def cpu_limited(host: dict) -> bool:
    """Whether a CPU ceiling is genuinely in force.

    Docker records it as NanoCpus; Podman as a quota over a period.
    Either is proof, neither being present is not.
    """
    if host.get("NanoCpus") == NANO_CPUS:
        return True
    quota, period = host.get("CpuQuota"), host.get("CpuPeriod")
    return bool(quota) and bool(period) and quota == CPU_QUOTA and period == CPU_PERIOD


def assert_policy_applied(attrs: dict, sandbox_id: str) -> None:
    """Confirm the engine actually applied what we asked for.

    An engine that accepts a config and silently ignores half of it
    hands back a container we would go on to DESCRIBE as limited.
    That is the worst failure mode available to this project: the
    agent is told it is sandboxed and it is not. So the limits are
    read back off the real container and a mismatch is fatal.
    """
    host = attrs.get("HostConfig") or {}
    config = attrs.get("Config") or {}
    expected = {"Memory": MEM_LIMIT_BYTES, "PidsLimit": PIDS_LIMIT}
    wrong = {
        key: host.get(key) for key, want in expected.items() if host.get(key) != want
    }
    if not cpu_limited(host):
        wrong["cpu"] = (
            f"NanoCpus={host.get('NanoCpus')} "
            f"CpuQuota={host.get('CpuQuota')} "
            f"CpuPeriod={host.get('CpuPeriod')}"
        )
    labels = config.get("Labels") or {}
    if labels.get(LABEL_ID) != sandbox_id:
        wrong["Labels"] = labels.get(LABEL_ID)
    if wrong:
        raise SandboxRuntimeError(
            "The container engine did not apply this server's resource "
            f"policy for sandbox '{sandbox_id}'. Expected {expected}, a "
            f"CPU ceiling of {CPUS} and label {sandbox_id}, but the "
            f"container reports {wrong}. Refusing to hand back a sandbox "
            "that is not actually limited."
        )


# --- network sealing --------------------------------------------------
#
# `network_disabled=True` is NOT usable here: it creates the container
# with no network sandbox at all, and Docker then refuses to attach one
# later (404 "network sandbox not found"). `network_mode="none"` fails
# the same way with a 400 on connect. Both were tried against a real
# container. What works is to let the container start on its normal
# network and immediately detach it, so a network can be re-attached
# for a build phase and detached again.


def attached_networks(container) -> list[str]:
    """Names of networks attached right now.

    Docker and Podman both expose NetworkSettings.Networks, but the key
    vanishes entirely once the last network is detached, so this must
    tolerate its absence rather than KeyError.
    """
    settings = container.attrs.get("NetworkSettings") or {}
    return list((settings.get("Networks") or {}).keys())


def seal(
    list_attached: Callable[[], list[str]],
    disconnect: Callable[[str], None],
    sandbox_id: str = "",
) -> None:
    """Detach every network. Fails closed: if we cannot seal, the caller
    must not be handed a sandbox we claim is sealed.

    Takes callables rather than a client, because the two runtimes have no
    common container type — one holds SDK objects, the other plain JSON
    from the REST API. Passing a shim that satisfies only part of a client
    interface is how the REST runtime came to hand a fake container object
    to docker-py, which tried to serialise it as JSON. Naming exactly the
    two operations needed makes that impossible.

    Still wrapped in with_retry: an idle engine drops connections, and
    sealing is as exposed to that as anything else.
    """
    def seal_once():
        for name in list_attached():
            disconnect(name)

    try:
        engine.with_retry(seal_once, "network sealing")
    except (EngineUnavailableError, ContainerGoneError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise SandboxRuntimeError(
            f"Could not seal network for '{sandbox_id}': "
            f"{type(exc).__name__}: {exc}"
        ) from exc


#: The default network each backend attaches containers to. Docker names
#: it "bridge", Podman names it "podman" — verified against both engines.
DEFAULT_NETWORK = {"docker": "bridge", "podman": "podman"}


def unseal(
    backend: str,
    list_available: Callable[[], list[str]],
    connect: Callable[[str], None],
) -> None:
    """Attach a network for a build phase.

    Docker calls its default "bridge"; Podman calls it "podman". Hardcoding
    either breaks the other, so the preferred name is chosen per backend and
    checked against what the engine actually offers before use.
    """
    preferred = DEFAULT_NETWORK.get(backend, "bridge")
    available = [n for n in list_available() if n and n != "none"]
    target = preferred if preferred in available else (
        available[0] if available else ""
    )
    if not target:
        raise SandboxRuntimeError(
            f"No usable network on backend '{backend}' for a dependency "
            "install."
        )
    connect(target)


# --- failure explanation ---------------------------------------------


def explain_sigkill(get_container: GetContainer) -> str:
    """Turn a bare 137 into something actionable."""
    try:
        container = get_container()
        if container.attrs.get("State", {}).get("OOMKilled"):
            return (
                f"Killed (SIGKILL): the sandbox exceeded its memory limit "
                f"of {MEM_LIMIT}. Reduce the working set, or process the "
                "data in chunks."
            )
    except Exception:  # noqa: BLE001 - explanation is best-effort
        pass
    return (
        "Killed (SIGKILL): the process was terminated by the sandbox, "
        f"most likely for exceeding the memory limit of {MEM_LIMIT} "
        f"or the process limit of {PIDS_LIMIT}."
    )


# --- reclamation ------------------------------------------------------


def too_young(container) -> bool:
    """Whether a container is inside the creation grace period.

    Unparseable or missing timestamps return False: an unknown age
    must not make a container permanently unreclaimable.
    """
    created = (container.attrs or {}).get("Created")
    if not isinstance(created, str) or not created:
        return False
    # Engines emit more fractional-second digits than fromisoformat
    # accepts on 3.11, so the fraction is trimmed to microseconds
    # while any timezone suffix is preserved.
    stamp = created.strip().replace("Z", "+00:00")
    match = re.match(
        r"^(?P<head>[\dT:-]+)" r"(?:\.(?P<frac>\d+))?" r"(?P<tz>[+-]\d{2}:?\d{2})?$",
        stamp,
    )
    if match:
        frac = (match.group("frac") or "")[:6]
        stamp = match.group("head")
        if frac:
            stamp += "." + frac.ljust(6, "0")
        stamp += match.group("tz") or "+00:00"
    try:
        born = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if born.tzinfo is None:
        born = born.replace(tzinfo=timezone.utc)
    # Read through the module, not from a name bound at import.
    #
    # `from policy import GC_GRACE_SECONDS` binds the VALUE, so every
    # module that did so kept its own private copy and rebinding one of
    # them changed nothing anywhere else. That is engineering-log section
    # 7 exactly, and it bit again here: moving this function into its own
    # module silently broke an acceptance test that had been shortening
    # the grace period by patching the copy in llm_sandbox_runtime. The
    # test was reaching into an implementation detail, and the detail
    # moved. Attribute access gives one source of truth for both.
    return (time.time() - born.timestamp()) < policy.GC_GRACE_SECONDS


def collect_orphans(known_ids: set[str], backends) -> list[str]:
    """Remove our containers whose sandbox_id is unknown to the
    registry. Matches on our labels only.

    Best effort by design: an engine that is not installed or not
    running is skipped rather than failing the sweep, because GC runs
    at server startup and must never stop the server from serving.
    """
    reclaimed: list[str] = []
    for backend in backends:
        try:
            containers = engine.list_managed(
                backend,
                f"{LABEL_MANAGED}=true",
                # A stopped engine must not stall a timed sweep for the
                # full interactive budget. GC probes BOTH backends every
                # time, so on a machine with only one of them installed
                # this is paid on every pass.
                cli_timeout=engine.PODMAN_CLI_TIMEOUT_FAST,
            )
        except (EngineUnavailableError, ContainerGoneError):
            continue
        for container in containers:
            labels = getattr(container, "labels", None) or {}
            sandbox_id = labels.get(LABEL_ID)
            if not sandbox_id or sandbox_id in known_ids:
                continue
            if too_young(container):
                # Another process may be creating this right now, in
                # the window before its registration lands.
                continue
            try:
                container.remove(force=True)
                reclaimed.append(sandbox_id)
            except Exception:  # noqa: BLE001 - best effort
                continue
    return reclaimed
