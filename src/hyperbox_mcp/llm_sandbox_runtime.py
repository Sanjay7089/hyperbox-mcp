"""The llm-sandbox implementation of the Runtime protocol.

THIS IS THE ONLY FILE ALLOWED TO IMPORT llm_sandbox. Every name and
signature below was verified directly against the installed package
(not assumed from docs) — see DESIGN.md's decision log:

- create_session(backend=SandboxBackend.X, lang=SupportedLanguage.Y)
  returns a session with explicit .open() / .close() (verified present),
  so we can hold it open across many .run() calls — a persistent
  sandbox, not a one-shot context manager.
- .run(code, libraries=..., timeout=...) -> ConsoleOutput with
  .stdout / .stderr / .exit_code.
- Top-level exceptions actually exported: SandboxError (base),
  ContainerError, ResourceError, SecurityError, ValidationError.
  (MissingDependencyError is NOT top-level — do not import it.)
- SandboxTimeoutError is NOT top-level either; it lives in
  llm_sandbox.exceptions and subclasses SandboxError, so it must be
  caught BEFORE the generic backend tuple or it disappears into it.
- create_session(container_id=...) attaches to an EXISTING container
  (_connect_to_existing_container, docker.py:287-312) and starts it if
  stopped. Verified by running code through a second session attached to
  a container the first session created. This is what makes a sandbox
  survive the process that made it.
- runtime_configs is forwarded verbatim into containers.create()
  (docker.py:378 -> 33), so labels/mem_limit/nano_cpus/pids_limit all
  land in HostConfig. Verified: labels present, Memory=536870912,
  NanoCpus=1000000000, PidsLimit=128.
- Verified against a real container: each .run() is a fresh process in
  the SAME container (differing os.getpid(), identical nodename). The
  filesystem and pip-installed packages persist between runs;
  interpreter memory does not.
- There is NO SupportedLanguage for bash/shell. Not our concern here.
"""

from __future__ import annotations

import uuid

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

from hyperbox_mcp.runtime import ExecResult, SandboxHandle

# These maps hold ONLY what has actually been run against a real
# container. java/cpp/r are free from llm-sandbox and have snippet sets
# ready in tests/verify.py, but an entry here is a promise the tool can
# deliver that environment, so nothing is added until a real verified
# run passes. See REQUIREMENTS.md Phase 1.
_LANGUAGES = {
    "python": SupportedLanguage.PYTHON,
    "javascript": SupportedLanguage.JAVASCRIPT,
    "ruby": SupportedLanguage.RUBY,
    "go": SupportedLanguage.GO,
}

_BACKENDS = {
    "docker": SandboxBackend.DOCKER,
    "podman": SandboxBackend.PODMAN,
}

# Every container we create carries both labels. GC matches on them and
# ONLY on them — a container without our label is never ours to remove.
LABEL_MANAGED = "hyperbox-mcp.managed"
LABEL_ID = "hyperbox-mcp.id"

# The default network each backend attaches containers to. Docker names
# it "bridge", Podman names it "podman" — verified against both engines.
_DEFAULT_NETWORK = {"docker": "bridge", "podman": "podman"}

# Server policy, not an agent's choice. See REQUIREMENTS.md Phase 6.
MEM_LIMIT = "1g"
NANO_CPUS = 1_000_000_000  # 1 CPU
PIDS_LIMIT = 128

# A no-op per language, used to run a dependency install without also
# running the caller's code while the network is briefly attached.
_NOOP = {
    "python": "pass",
    "javascript": "0;",
    "ruby": "nil",
    "go": "package main\nfunc main() {}",
}

_BACKEND_EXCEPTIONS = (
    SandboxError,
    ContainerError,
    ResourceError,
    SecurityError,
    ValidationError,
)


class UnsupportedLanguageError(ValueError):
    pass


class UnsupportedBackendError(ValueError):
    pass


class SandboxRuntimeError(RuntimeError):
    """Wraps any backend exception so callers never see raw llm-sandbox
    types — keeps the backend replaceable."""


def _engine_client(backend: str):
    """A raw engine client for label queries GC needs. Imported lazily so
    a missing podman install never breaks the docker path."""
    if backend == "docker":
        import docker

        return docker.from_env()
    if backend == "podman":
        from podman import PodmanClient

        return PodmanClient.from_env()
    raise UnsupportedBackendError(f"Unsupported backend '{backend}'")


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

    def _networks(self, handle: SandboxHandle):
        client = _engine_client(handle.backend)
        container = client.containers.get(handle.meta["container_ref"])
        container.reload()
        return client, container

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
            client, container = self._networks(handle)
            for name in self._attached_networks(container):
                client.networks.get(name).disconnect(container)
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
        client, container = self._networks(handle)
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

    def create(self, language: str, backend: str) -> SandboxHandle:
        self._validate(language, backend)

        # The id is minted before the container so it can be baked into
        # the labels GC matches on.
        sandbox_id = uuid.uuid4().hex[:12]
        try:
            session = create_session(
                backend=_BACKENDS[backend],
                lang=_LANGUAGES[language],
                runtime_configs={
                    "labels": {LABEL_MANAGED: "true", LABEL_ID: sandbox_id},
                    "mem_limit": MEM_LIMIT,
                    "nano_cpus": NANO_CPUS,
                    "pids_limit": PIDS_LIMIT,
                },
            )
            session.open()
        except _BACKEND_EXCEPTIONS as exc:
            raise SandboxRuntimeError(f"{type(exc).__name__}: {exc}") from exc

        container_ref = getattr(getattr(session, "container", None), "id", None)
        if not container_ref:
            # Without a container ref the sandbox cannot outlive this
            # process, which defeats the registry. Fail loudly instead of
            # handing back a handle that silently degrades.
            try:
                session.close()
            except Exception:  # noqa: BLE001 - already failing; don't mask
                pass
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
        # Sealed by default. If this fails the sandbox is destroyed rather
        # than returned with network access nobody asked for.
        try:
            self._seal(handle)
        except SandboxRuntimeError:
            self.destroy(handle)
            raise
        return handle

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
                self._seal(handle)
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
            _, container = self._networks(handle)
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
        """Ask the engine, not our own bookkeeping."""
        container_ref = handle.meta.get("container_ref")
        if not container_ref:
            return False
        try:
            client = _engine_client(handle.backend)
            container = client.containers.get(container_ref)
            return getattr(container, "status", "") == "running"
        except Exception:  # noqa: BLE001 - absent, unreachable, or gone
            return False

    def destroy(self, handle: SandboxHandle) -> None:
        """Tear the container down by reference, not by session ownership.

        session.close() only removes a container the session CREATED —
        `if not self.using_existing_container` (docker.py:418). A process
        that reattached would therefore detach and leave the container
        running, which is exactly how orphans accumulated. Destroy means
        destroy, whichever process is asking, so we close the session for
        tidiness and then remove the container explicitly.
        """
        session = self._sessions.pop(handle.sandbox_id, None)
        if session is not None:
            try:
                session.close()
            except _BACKEND_EXCEPTIONS:
                pass  # removal below is the operation that matters

        container_ref = handle.meta.get("container_ref")
        if not container_ref:
            return  # nothing durable to remove
        try:
            client = _engine_client(handle.backend)
            container = client.containers.get(container_ref)
        except Exception:  # noqa: BLE001 - already gone, or engine down
            return
        try:
            container.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            raise SandboxRuntimeError(
                f"Failed to remove container for '{handle.sandbox_id}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def gc(self, known_ids: set[str]) -> list[str]:
        """Remove our containers whose sandbox_id is unknown to the
        registry. Matches on our labels only."""
        reclaimed: list[str] = []
        for backend in _BACKENDS:
            try:
                client = _engine_client(backend)
                containers = client.containers.list(
                    all=True, filters={"label": f"{LABEL_MANAGED}=true"}
                )
            except Exception:  # noqa: BLE001 - engine absent/unreachable
                continue
            for container in containers:
                labels = getattr(container, "labels", None) or {}
                sandbox_id = labels.get(LABEL_ID)
                if not sandbox_id or sandbox_id in known_ids:
                    continue
                try:
                    container.remove(force=True)
                    reclaimed.append(sandbox_id)
                except Exception:  # noqa: BLE001 - best effort
                    continue
        return reclaimed
