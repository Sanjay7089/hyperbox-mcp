"""The llm-sandbox implementation of the Runtime protocol.

THIS IS THE ONLY FILE ALLOWED TO IMPORT llm_sandbox. Every name and
signature below was verified against the installed package rather than
assumed from its docs:

- create_session(backend=..., lang=...) returns a session with explicit
  .open() / .close(), so one sandbox stays open across many .run() calls.
- .run(code, libraries=..., timeout=...) -> ConsoleOutput with
  .stdout / .stderr / .exit_code.
- Top-level exceptions actually exported: SandboxError (base),
  ContainerError, ResourceError, SecurityError, ValidationError.
  SandboxTimeoutError is NOT top-level; it lives in llm_sandbox.exceptions
  and subclasses SandboxError, so it must be caught BEFORE the generic
  tuple or it disappears into it.
- create_session(container_id=...) attaches to an EXISTING container and
  starts it if stopped. This is what lets a sandbox survive the process
  that made it.
- runtime_configs is forwarded verbatim into containers.create(), so
  labels / mem_limit / nano_cpus / pids_limit / tmpfs / security_opt all
  land in the real HostConfig. Asserted after creation, not trusted.
- Each .run() is a fresh process in the SAME container. The filesystem
  and installed packages persist between runs; interpreter memory does
  not.

Measured limits of hardening this backend (see docs/security-model.md):

- A non-root `user` makes the container unusable. llm-sandbox provisions
  a virtualenv at /sandbox/.sandbox-venv during environment setup, which
  needs root in these images; as uid 1000 every subsequent exec fails
  with 127 because the interpreter was never created.
- cap_drop: ["ALL"] breaks it too, even as root: dropping
  CAP_DAC_OVERRIDE removes root's permission-bypass, so llm-sandbox
  cannot read the file it just copied into /sandbox (Errno 13).
Both were tried against a real container and reverted. What survives is
applied below.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from llm_sandbox import (
    ContainerError,
    ResourceError,
    SandboxBackend,
    SandboxError,
    SecurityError,
    SupportedLanguage,
    ValidationError,
    create_session,
)
from llm_sandbox.exceptions import SandboxTimeoutError

from hyperbox_mcp import engine
from hyperbox_mcp.engine import ContainerGoneError, EngineUnavailableError
from hyperbox_mcp.policy import (
    GC_GRACE_SECONDS,
    LABEL_ID,
    LABEL_MANAGED,
    MEM_LIMIT,
    MEM_LIMIT_BYTES,
    NANO_CPUS,
    PIDS_LIMIT,
    SECURITY_OPT,
    TMPFS,
)
from hyperbox_mcp.runtime import ExecResult, SandboxHandle

_LANGUAGES = {
    "python": SupportedLanguage.PYTHON,
    # javascript / ruby / go are supported by llm-sandbox and have snippet
    # sets ready in tests/verify.py, but an entry here is a promise the
    # tool can deliver that environment. They return once each clears the
    # hardened suite against a real container.
}

_BACKENDS = {
    "docker": SandboxBackend.DOCKER,
    "podman": SandboxBackend.PODMAN,
}

# The default network each backend attaches containers to. Docker names
# it "bridge", Podman names it "podman" — verified against both engines.
_DEFAULT_NETWORK = {"docker": "bridge", "podman": "podman"}

# A no-op per language, used to run a dependency install without also
# running the caller's code while the network is briefly attached.
_NOOP = {"python": "pass"}

_BACKEND_EXCEPTIONS = (
    SandboxError,
    ContainerError,
    ResourceError,
    SecurityError,
    ValidationError,
)


class UnsupportedLanguageError(ValueError):
    pass


UnsupportedBackendError = engine.UnsupportedBackendError


class SandboxRuntimeError(RuntimeError):
    """Wraps any backend exception so callers never see raw llm-sandbox
    types — keeps the backend replaceable."""


class LLMSandboxRuntime:
    """Runtime implementation backed by llm-sandbox.

    Sessions are cached in-process for speed, but the cache is NOT the
    source of truth: given a handle carrying a container_ref, this class
    reattaches to a container it has never seen. That is what lets a
    second server process — or the same process after a restart — keep
    using a sandbox it did not create.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, object] = {}

    # --- helpers ------------------------------------------------------

    def _validate(self, language: str, backend: str) -> None:
        if language not in _LANGUAGES:
            raise UnsupportedLanguageError(
                f"Unsupported language '{language}'. Supported: "
                f"{', '.join(_LANGUAGES)}"
            )
        if backend not in _BACKENDS:
            raise UnsupportedBackendError(
                f"Unsupported backend '{backend}'. Supported: "
                f"{', '.join(_BACKENDS)}"
            )

    def _runtime_configs(self, sandbox_id: str) -> dict:
        """Everything the engine must apply. Server policy, start to
        finish — no part of this comes from a caller."""
        return {
            "labels": {LABEL_MANAGED: "true", LABEL_ID: sandbox_id},
            "mem_limit": MEM_LIMIT,
            "nano_cpus": NANO_CPUS,
            "pids_limit": PIDS_LIMIT,
            "tmpfs": dict(TMPFS),
            "security_opt": list(SECURITY_OPT),
        }

    @staticmethod
    def _assert_policy_applied(attrs: dict, sandbox_id: str) -> None:
        """Confirm the engine actually applied what we asked for.

        An engine that accepts a config and silently ignores half of it
        hands back a container we would go on to DESCRIBE as limited.
        That is the worst failure mode available to this project: the
        agent is told it is sandboxed and it is not. So the limits are
        read back off the real container and a mismatch is fatal.
        """
        host = attrs.get("HostConfig") or {}
        config = attrs.get("Config") or {}
        expected = {
            "Memory": MEM_LIMIT_BYTES,
            "NanoCpus": NANO_CPUS,
            "PidsLimit": PIDS_LIMIT,
        }
        wrong = {
            key: host.get(key)
            for key, want in expected.items()
            if host.get(key) != want
        }
        labels = config.get("Labels") or {}
        if labels.get(LABEL_ID) != sandbox_id:
            wrong["Labels"] = labels.get(LABEL_ID)
        if wrong:
            raise SandboxRuntimeError(
                "The container engine did not apply this server's resource "
                f"policy for sandbox '{sandbox_id}'. Expected "
                f"{expected} with label {sandbox_id}, but the container "
                f"reports {wrong}. Refusing to hand back a sandbox that is "
                "not actually limited."
            )

    def _container(self, handle: SandboxHandle):
        ref = handle.meta.get("container_ref")
        if not ref:
            raise ContainerGoneError(
                f"Sandbox '{handle.sandbox_id}' has no container reference."
            )
        return engine.get_container(handle.backend, ref)

    # --- network sealing ----------------------------------------------
    #
    # `network_disabled=True` is NOT usable here: it creates the container
    # with no network sandbox at all, and Docker then refuses to attach one
    # later (404 "network sandbox not found"). `network_mode="none"` fails
    # the same way with a 400 on connect. Both were tried against a real
    # container. What works is to let the container start on its normal
    # network and immediately detach it, so a network can be re-attached
    # for a build phase and detached again.
    #
    # This leaves a brief window between container start and _seal() in
    # which the container has a network. No caller-supplied code runs in
    # that window — only llm-sandbox's own environment setup — so nothing
    # an agent submits is ever executed unsealed.

    @staticmethod
    def _attached_networks(container) -> list[str]:
        """Names of networks attached right now.

        Docker and Podman both expose NetworkSettings.Networks, but the key
        vanishes entirely once the last network is detached, so this must
        tolerate its absence rather than KeyError.
        """
        settings = container.attrs.get("NetworkSettings") or {}
        return list((settings.get("Networks") or {}).keys())

    def _seal(self, handle: SandboxHandle) -> None:
        """Detach every network. Fails closed: if we cannot seal, the
        caller must not be handed a sandbox we claim is sealed."""
        try:
            client = engine.client(handle.backend)
            container = self._container(handle)
            container.reload()
            for name in self._attached_networks(container):
                client.networks.get(name).disconnect(container)
        except (EngineUnavailableError, ContainerGoneError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxRuntimeError(
                f"Could not seal network for '{handle.sandbox_id}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _unseal(self, handle: SandboxHandle) -> None:
        """Attach the backend's default network for a build phase.

        Docker calls it "bridge"; Podman calls it "podman". Hardcoding
        either one breaks the other backend, so the name is chosen per
        backend and verified against the engine before use.
        """
        client = engine.client(handle.backend)
        container = self._container(handle)
        preferred = _DEFAULT_NETWORK.get(handle.backend, "bridge")
        try:
            network = client.networks.get(preferred)
        except Exception:  # noqa: BLE001 - fall back to whatever exists
            available = [getattr(n, "name", "") for n in client.networks.list()]
            usable = [n for n in available if n and n != "none"]
            if not usable:
                raise SandboxRuntimeError(
                    f"No usable network on backend '{handle.backend}' for a "
                    "dependency install."
                ) from None
            network = client.networks.get(usable[0])
        network.connect(container)

    def _session_for(self, handle: SandboxHandle):
        """Return a live session for `handle`, reattaching to its
        container if this process has never seen it."""
        session = self._sessions.get(handle.sandbox_id)
        if session is not None:
            return session

        container_ref = handle.meta.get("container_ref")
        if not container_ref:
            raise SandboxRuntimeError(
                f"No live sandbox '{handle.sandbox_id}' and no container "
                "reference to reattach to. Create one first."
            )
        # Confirm the container is really there before llm-sandbox tries
        # to attach, so an unreachable engine surfaces as itself rather
        # than as a confusing backend error.
        self._container(handle)
        try:
            session = create_session(
                backend=_BACKENDS[handle.backend],
                lang=_LANGUAGES[handle.language],
                container_id=container_ref,
            )
            session.open()
        except _BACKEND_EXCEPTIONS as exc:
            raise SandboxRuntimeError(
                f"Could not reattach to sandbox '{handle.sandbox_id}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._sessions[handle.sandbox_id] = session
        return session

    # --- Runtime protocol ---------------------------------------------

    def create(
        self, language: str, backend: str, sandbox_id: str
    ) -> SandboxHandle:
        self._validate(language, backend)
        # Fail on an unreachable engine before allocating anything, with
        # that engine's own actionable fix rather than a generic error.
        engine.client(backend)

        try:
            session = create_session(
                backend=_BACKENDS[backend],
                lang=_LANGUAGES[language],
                runtime_configs=self._runtime_configs(sandbox_id),
            )
            session.open()
        except _BACKEND_EXCEPTIONS as exc:
            raise SandboxRuntimeError(f"{type(exc).__name__}: {exc}") from exc

        container_ref = getattr(getattr(session, "container", None), "id", None)
        if not container_ref:
            # Without a container ref the sandbox cannot outlive this
            # process, which defeats the registry. Fail loudly instead of
            # handing back a handle that silently degrades.
            self._close_quietly(session)
            raise SandboxRuntimeError(
                "Backend did not expose a container id; cannot register "
                "a durable sandbox."
            )

        self._sessions[sandbox_id] = session
        handle = SandboxHandle(
            sandbox_id=sandbox_id,
            language=language,
            backend=backend,
            meta={"container_ref": container_ref},
        )
        # Everything past this point either succeeds or takes the
        # container with it. A half-configured sandbox is never returned.
        try:
            container = self._container(handle)
            self._assert_policy_applied(container.attrs, sandbox_id)
            self._seal(handle)
        except BaseException:
            self._destroy_quietly(handle)
            raise
        return handle

    def _close_quietly(self, session) -> None:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - already failing; do not mask
            pass

    def _destroy_quietly(self, handle: SandboxHandle) -> None:
        try:
            self.destroy(handle)
        except Exception:  # noqa: BLE001 - already failing; do not mask
            pass

    def run(
        self,
        handle: SandboxHandle,
        code: str,
        libraries: list[str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        session = self._session_for(handle)

        # Build phase: the ONLY time a network exists. Dependencies are
        # installed with the network attached and the caller's code is not
        # run yet; the network is detached before the code executes, so
        # submitted code never runs with network access.
        if libraries:
            try:
                self._unseal(handle)
                install = session.run(
                    _NOOP.get(handle.language, "pass"),
                    libraries=libraries,
                    timeout=timeout,
                )
            except _BACKEND_EXCEPTIONS as exc:
                return ExecResult(
                    stdout="",
                    stderr=f"Dependency install failed: {type(exc).__name__}: {exc}",
                    exit_code=-1,
                )
            finally:
                self._seal(handle)
            if install.exit_code != 0:
                return ExecResult(
                    stdout=install.stdout,
                    stderr=f"Dependency install failed:\n{install.stderr}",
                    exit_code=install.exit_code,
                )

        try:
            out = session.run(code, timeout=timeout)
        except SandboxTimeoutError as exc:
            # Caught before _BACKEND_EXCEPTIONS, which would otherwise
            # swallow it (it subclasses SandboxError). A timeout is a
            # distinct outcome: the code hung rather than failed, and
            # the agent's next move differs accordingly.
            return ExecResult(
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                exit_code=-1,
                timed_out=True,
            )
        except _BACKEND_EXCEPTIONS as exc:
            # A backend error running code is a structured failure the
            # agent should reason about, not a crash — return it as one.
            return ExecResult(
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                exit_code=-1,
            )
        stderr = out.stderr
        if out.exit_code == 137 and not stderr.strip():
            # A cgroup OOM kill arrives as a bare SIGKILL with no output at
            # all, which tells a reasoning agent nothing about why its code
            # died. Ask the engine what happened and say so.
            stderr = self._explain_sigkill(handle)
        return ExecResult(
            stdout=out.stdout,
            stderr=stderr,
            exit_code=out.exit_code,
        )

    def _explain_sigkill(self, handle: SandboxHandle) -> str:
        """Turn a bare 137 into something actionable."""
        try:
            container = self._container(handle)
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

    def alive(self, handle: SandboxHandle) -> bool:
        """Ask the engine, not our own bookkeeping.

        Raises EngineUnavailableError rather than returning False when the
        engine cannot be reached — False here means the engine answered.
        """
        try:
            container = self._container(handle)
        except ContainerGoneError:
            return False
        return getattr(container, "status", "") == "running"

    def destroy(self, handle: SandboxHandle) -> None:
        """Tear the container down by reference, not by session ownership.

        session.close() only removes a container the session CREATED, so a
        process that reattached would detach and leave the container
        running — exactly how orphans accumulated. Destroy means destroy,
        whichever process is asking, so we close the session for tidiness
        and then remove the container explicitly.

        Returns only on confirmed absence. An unreachable engine raises,
        so the caller keeps its registry row instead of forgetting a
        container that may still be running.
        """
        session = self._sessions.pop(handle.sandbox_id, None)
        if session is not None:
            try:
                session.close()
            except _BACKEND_EXCEPTIONS:
                pass  # removal below is the operation that matters

        try:
            container = self._container(handle)
        except ContainerGoneError:
            return  # the engine confirmed it: nothing left to remove

        try:
            container.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            if engine.is_not_found(handle.backend, exc):
                return  # removed by someone else between get and remove
            raise SandboxRuntimeError(
                f"Failed to remove container for '{handle.sandbox_id}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def gc(self, known_ids: set[str]) -> list[str]:
        """Remove our containers whose sandbox_id is unknown to the
        registry. Matches on our labels only.

        Best effort by design: an engine that is not installed or not
        running is skipped rather than failing the sweep, because GC runs
        at server startup and must never stop the server from serving.
        """
        reclaimed: list[str] = []
        for backend in _BACKENDS:
            try:
                containers = engine.list_managed(
                    backend, f"{LABEL_MANAGED}=true"
                )
            except (EngineUnavailableError, ContainerGoneError):
                continue
            for container in containers:
                labels = getattr(container, "labels", None) or {}
                sandbox_id = labels.get(LABEL_ID)
                if not sandbox_id or sandbox_id in known_ids:
                    continue
                if self._too_young(container):
                    # Another process may be creating this right now, in
                    # the window before its registration lands.
                    continue
                try:
                    container.remove(force=True)
                    reclaimed.append(sandbox_id)
                except Exception:  # noqa: BLE001 - best effort
                    continue
        return reclaimed

    @staticmethod
    def _too_young(container) -> bool:
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
            r"^(?P<head>[\dT:-]+)"
            r"(?:\.(?P<frac>\d+))?"
            r"(?P<tz>[+-]\d{2}:?\d{2})?$",
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
        return (time.time() - born.timestamp()) < GC_GRACE_SECONDS
