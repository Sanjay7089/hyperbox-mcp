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

Two standard hardening measures do NOT work against this backend and
were reverted after being measured: a non-root `user` (the backend needs
root to provision its virtualenv, so every exec then fails with 127) and
cap_drop: ["ALL"] (without CAP_DAC_OVERRIDE it cannot read the file it
just copied into /sandbox). What survives is applied below and explained
in docs/security.md.
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

from hyperbox_mcp import engine, errors, policy, sandbox_ops
from hyperbox_mcp.engine import ContainerGoneError, EngineUnavailableError
from hyperbox_mcp.policy import (
    CPU_PERIOD,
    CPU_QUOTA,
    GC_GRACE_SECONDS,
    LABEL_ID,
    LABEL_MANAGED,
    MEM_LIMIT,
    CPUS,
    MEM_LIMIT_BYTES,
    NANO_CPUS,
    NO_NEW_PRIVILEGES,
    PIDS_LIMIT,
    TMPFS_PATHS,
    TMPFS_SIZE,
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

#: Proof that results actually round-trip out of this container.
#:
#: This exists because of a measured failure: talking to Podman over the
#: TCP port its machine forwards, every exec returns the correct exit
#: code and zero bytes of output. The result is a run that reports
#: success with empty stdout — a sandbox that silently discards every
#: result it is asked to produce. An agent reading that would conclude
#: its code printed nothing, and go on to debug code that was fine.
#: engine.ensure_podman_transport now prevents that transport from being
#: chosen; this check stays because it catches the whole class, and it
#: is what caught this one.
#:
#: So every sandbox proves it can return a result before it is handed
#: out, on every backend. One extra exec at creation is cheap; a
#: silently mute sandbox is not.
CANARY_MARKER = "__hyperbox_canary__"
_CANARY = {"python": f"print('{CANARY_MARKER}')"}

#: Find, and kill, the processes running submitted code.
#:
#: Read /proc directly with the interpreter that is already PID 1 in these
#: images. `ps` and `pkill` live in procps, which the slim language images
#: do not install — and a kill that depends on a binary the image may not
#: have is a kill that silently does nothing, which is the failure mode this
#: whole module is written against.
#:
#: The match is on the path the backend writes snippets to, so only
#: submitted code is ever a target: never PID 1, never the backend's own
#: environment setup.
_SCAN_PROC = """
import os
me = os.getpid()
hits = []
for entry in os.listdir('/proc'):
    if not entry.isdigit() or int(entry) == me:
        continue
    try:
        with open('/proc/' + entry + '/cmdline', 'rb') as fh:
            line = fh.read().decode('utf-8', 'replace')
    except OSError:
        continue
    if '/sandbox/' in line and '.py' in line:
        hits.append(int(entry))
"""

_LIST_SANDBOX_PIDS = _SCAN_PROC + "print(' '.join(str(p) for p in hits))\n"

_KILL_SANDBOX_PIDS = _SCAN_PROC + """
import signal
for pid in hits:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
"""

#: How long to wait for a restarted container to report itself running.
RESTART_POLL_ATTEMPTS = 10
RESTART_POLL_SECONDS = 0.5

_BACKEND_EXCEPTIONS = (
    SandboxError,
    ContainerError,
    ResourceError,
    SecurityError,
    ValidationError,
)


#: Both defined in errors.py, still ValueErrors. Kept distinct from each
#: other on purpose: an unknown environment tells the agent to build one,
#: an unsupported language tells it to pick another.
UnsupportedLanguageError = errors.UnsupportedLanguageError
UnsupportedEnvironmentError = errors.UnknownEnvironmentError


UnsupportedBackendError = engine.UnsupportedBackendError


#: Defined in sandbox_ops so both runtimes raise the same type; every
#: existing `except SandboxRuntimeError` keeps working unchanged.
SandboxRuntimeError = sandbox_ops.SandboxRuntimeError


def image_for(language: str) -> str:
    """The image a sandbox of this language needs, as the backend would
    resolve it. Read from llm-sandbox rather than hardcoded, so it cannot
    drift from what actually gets pulled."""
    from llm_sandbox.const import DefaultImage

    return getattr(DefaultImage, language.upper())


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
        # Sandboxes whose output has been proved to reach us, this
        # process. Checked once per sandbox rather than on every run.
        self._canary_verified: set[str] = set()

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

    @staticmethod
    def _engine_specific(backend: str) -> dict:
        return sandbox_ops.engine_specific(backend)

    def _runtime_configs(self, sandbox_id: str, backend: str) -> dict:
        return sandbox_ops.runtime_configs(sandbox_id, backend)

    @staticmethod
    def _cpu_limited(host: dict) -> bool:
        return sandbox_ops.cpu_limited(host)

    @staticmethod
    def _assert_policy_applied(attrs: dict, sandbox_id: str) -> None:
        sandbox_ops.assert_policy_applied(attrs, sandbox_id)

    def _container(self, handle: SandboxHandle):
        """Look up this sandbox's container.

        Recovery from a connection the engine closed underneath us lives
        in engine.with_retry, so every engine call gets it rather than
        just this one.
        """
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
        return sandbox_ops.attached_networks(container)

    def _seal(self, handle: SandboxHandle) -> None:
        sandbox_ops.seal(
            handle.backend, lambda: self._container(handle), handle.sandbox_id
        )

    def _unseal(self, handle: SandboxHandle) -> None:
        sandbox_ops.unseal(handle.backend, lambda: self._container(handle))

    def _session_for(self, handle: SandboxHandle):
        """Return a live session for `handle`, reattaching to its
        container if this process has never seen it.

        The first time this process opens a given sandbox it also proves
        the sandbox can return output at all — see _verify_once.
        """
        session = self._sessions.get(handle.sandbox_id)
        if session is not None:
            self._verify_once(handle)
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
        extra = engine.session_kwargs(handle.backend)
        session_backend = extra.pop("session_backend", handle.backend)
        try:
            session = create_session(
                backend=_BACKENDS[session_backend],
                lang=_LANGUAGES[handle.language],
                container_id=container_ref,
                keep_template=True,  # see create(); never delete our image
                **extra,
            )
            session.open()
        except _BACKEND_EXCEPTIONS as exc:
            raise SandboxRuntimeError(
                f"Could not reattach to sandbox '{handle.sandbox_id}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._sessions[handle.sandbox_id] = session
        self._verify_once(handle)
        return session

    def _verify_once(self, handle: SandboxHandle) -> None:
        """Run the output round-trip check once per sandbox, per process.

        The ordering here is load-bearing and was got wrong once: the id
        goes into the set BEFORE the check runs, not after. The check
        itself calls run(), which calls _session_for(), which calls this
        again — so marking afterwards means the guard is never set on the
        re-entrant path and the two functions recurse until the stack
        gives out. Marking first makes the inner call a no-op.

        On failure the mark is removed, so a later attempt (or a fresh
        process) can retry rather than inheriting a permanent verdict.
        """
        sandbox_id = handle.sandbox_id
        if sandbox_id in self._canary_verified:
            return
        self._canary_verified.add(sandbox_id)
        try:
            self._assert_results_round_trip(handle)
        except BaseException:
            self._canary_verified.discard(sandbox_id)
            raise

    # --- Runtime protocol ---------------------------------------------

    def create(
        self, language: str, backend: str, sandbox_id: str,
        environment: str | None = None,
    ) -> SandboxHandle:
        self._validate(language, backend)
        # Resolved ONCE, and before anything is allocated. Calling
        # policy.environments() twice would leave a window in which a
        # concurrent `hyperbox build` changes the map between the
        # membership check and the lookup, turning a clean error into a
        # KeyError.
        image = None
        if environment:
            available = policy.environments()
            if environment not in available:
                raise UnsupportedEnvironmentError(
                    f"Unknown environment '{environment}'. "
                    f"Available: {', '.join(sorted(available))}. "
                    "Build one with: hyperbox build <name> --custom <Dockerfile>"
                )
            image = available[environment]

        # Fail on an unreachable engine before allocating anything, with
        # that engine's own actionable fix rather than a generic error.
        engine.client(backend)

        # On Windows a Podman sandbox runs through the Docker session
        # class against Podman's Docker-compatible pipe; everywhere else
        # this is empty and the backend speaks for itself.
        extra = engine.session_kwargs(backend)
        session_backend = extra.pop("session_backend", backend)
        # Only the base image changes. runtime_configs still carries every
        # limit, so _assert_policy_applied still reads them back off the
        # real container and the canary still refuses a mute sandbox.
        if image:
            extra["image"] = image
        try:
            session = create_session(
                backend=_BACKENDS[session_backend],
                lang=_LANGUAGES[language],
                runtime_configs=self._runtime_configs(sandbox_id, backend),
                # Without this the backend DELETES the language image when
                # the session closes, whenever that session was the one
                # that pulled it (_get_or_pull_image sets is_create_template
                # on a pull, and close() then calls _cleanup_image if no
                # container references the image any more). destroy() calls
                # close() before removing the container, so the check
                # passes. The result for one-sandbox-at-a-time use is a
                # multi-gigabyte re-pull on every single create.
                keep_template=True,
                **extra,
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
        # The output round-trip check deliberately does NOT run here. It
        # costs a full exec, and creation is already the slowest thing
        # this server does — on a cold machine it pulls gigabytes, and
        # every second spent here is a second closer to the client's
        # timeout. It runs instead on first use, in _session_for.
        return handle

    def _assert_results_round_trip(self, handle: SandboxHandle) -> None:
        """Refuse to hand back a sandbox that cannot report results.

        Runs a marker through exactly the path a caller's code takes. If
        it does not come back, the sandbox would answer every future run
        with empty output — so it fails here, loudly and once, instead of
        silently on every call.
        """
        probe = _CANARY.get(handle.language)
        if probe is None:
            return
        try:
            result = self.run(handle, probe, timeout=30)
        except Exception as exc:  # noqa: BLE001 - reported as our own error
            raise SandboxRuntimeError(
                f"Sandbox '{handle.sandbox_id}' on {handle.backend} could not "
                f"run a startup check: {type(exc).__name__}: {exc}"
            ) from exc
        if CANARY_MARKER in result.stdout:
            return
        raise SandboxRuntimeError(
            f"The {handle.backend} backend started a container but its output "
            f"does not reach this server: a startup check printed "
            f"'{CANARY_MARKER}' and returned exit code {result.exit_code} with "
            f"stdout={result.stdout!r} stderr={result.stderr!r}. Every run in "
            "this sandbox would report success with empty output, so it is "
            "refused rather than handed back. On Podman this means the "
            "client is talking over a TCP forward instead of the machine's "
            "unix socket: export CONTAINER_HOST=\"unix://$(podman machine "
            "inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}')\" "
            "and retry, or use backend='docker'."
        )

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
            except SandboxTimeoutError as exc:
                # Before _BACKEND_EXCEPTIONS, which it subclasses. A hung
                # install burns the same CPU a hung program does, and used
                # to be reported as a generic failure while pip kept going.
                return ExecResult(
                    stdout="",
                    stderr=f"Dependency install timed out: {exc}."
                    + self._kill_runaway(handle),
                    exit_code=-1,
                    timed_out=True,
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
                stderr=f"{type(exc).__name__}: {exc}."
                + self._kill_runaway(handle),
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

    @staticmethod
    def _exec(container, argv: list[str]) -> tuple[int, str]:
        """Run a command in the container, returning (exit_code, stdout).

        Flattens two separate client disagreements, both of which produce
        wrong answers rather than errors:

        - docker-py returns an ExecResult; podman-py returns a plain
          (exit_code, output) tuple.
        - docker-py demultiplexes the exec stream; podman-py hands back the
          raw framed bytes. Decoding those directly yields a string of
          header bytes that reads as output. See engine.demux_frames.
        """
        result = container.exec_run(argv)
        code, output = (
            result if isinstance(result, tuple)
            else (result.exit_code, result.output)
        )
        if output is None:
            output = b""
        elif not isinstance(output, (bytes, bytearray)):
            # A streamed exec yields chunks. Join them rather than str()ing
            # the iterator, which produces "<generator object ...>" and
            # looks like output.
            try:
                output = b"".join(output)
            except TypeError:
                output = b""
        stdout, _ = engine.demux_frames(bytes(output))
        return (code or 0), stdout

    def _sandbox_processes(self, container) -> list[str]:
        """PIDs inside the container that are running submitted code.

        Read from /proc with the interpreter that is already PID 1 in these
        images, rather than with `ps` or `pkill`: procps is not installed in
        the slim language images, and a kill that depends on a binary the
        image may not have is a kill that silently does nothing.
        """
        code, out = self._exec(
            container,
            ["python3", "-c", _LIST_SANDBOX_PIDS],
        )
        if code != 0:
            return []
        return [line for line in out.split() if line.strip().isdigit()]

    def _kill_runaway(self, handle: SandboxHandle) -> str:
        """Stop code that outlived its timeout, and say what stopping cost.

        The backend's timeout does not stop anything. Its TimeoutMixin is a
        host-side thread.join, and the container-level cancellation its own
        docstring promises is a no-op for a container the session created
        and a bare detach for one it reattached to. So a `while True: pass`
        keeps running at the full CPU ceiling until the sandbox is
        destroyed, and a reattached session is left unusable afterwards.

        Two levers, tried in order:

        1. Kill the submitted code's processes directly, found by reading
           /proc through the interpreter that is already PID 1. This leaves
           the container up, so the filesystem — including /work — survives
           and the network stays sealed exactly as it was.
        2. Restart the container, if anything survived that. Blunt: it
           empties the tmpfs mounts and needs the seal re-applied, so it is
           the fallback rather than the plan. The restart is then VERIFIED
           and started explicitly if the engine left it stopped, because a
           sandbox that quietly fails to come back is worse than the runaway
           it replaced.

        Best effort throughout: a timeout is already being reported, and
        failing to tidy up must not replace that with a less useful error.
        The session is dropped either way, so the next run reattaches to
        whatever state the container is really in.
        """
        self._sessions.pop(handle.sandbox_id, None)
        self._canary_verified.discard(handle.sandbox_id)
        try:
            container = self._container(handle)
        except Exception as exc:  # noqa: BLE001 - already reporting a timeout
            return (
                f" The sandbox could not be reached to stop it "
                f"({type(exc).__name__}: {exc}), so that code may still be "
                "running. Call destroy_sandbox to be certain."
            )

        try:
            self._exec(container, ["python3", "-c", _KILL_SANDBOX_PIDS])
            survivors = self._sandbox_processes(container)
        except Exception:  # noqa: BLE001 - fall through to the restart
            survivors = ["unknown"]
        if not survivors:
            return " The code was killed; the sandbox is still usable."

        return self._restart_to_kill(handle, container)

    def _restart_to_kill(self, handle: SandboxHandle, container) -> str:
        """Last resort: take the whole container down and back up."""
        try:
            # An explicit, short stop grace. The default is ten seconds of
            # SIGTERM politeness aimed at PID 1, but the runaway is an exec
            # child, so nothing useful happens during that wait — a 5s
            # timeout was measured returning after 19s.
            container.restart(timeout=2)
        except Exception as exc:  # noqa: BLE001
            return (
                f" The sandbox could not be restarted to stop it "
                f"({type(exc).__name__}: {exc}), so that code may still be "
                "running. Call destroy_sandbox to be certain."
            )

        # Verify rather than assume. A restart that reports success and
        # leaves the container stopped was observed on Docker while an exec
        # was in flight, and an unusable sandbox reported as recovered is
        # exactly the confident-wrong-answer this project exists to avoid.
        note = (
            " The sandbox was restarted to stop the code, so scratch space "
            f"({', '.join(TMPFS_PATHS)}) is now empty; the rest of its "
            "filesystem, including installed packages, survives."
        )
        for _ in range(RESTART_POLL_ATTEMPTS):
            try:
                container.reload()
                if getattr(container, "status", "") == "running":
                    break
                container.start()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(RESTART_POLL_SECONDS)
        else:
            return (
                note
                + " WARNING: it did not come back up. Call destroy_sandbox "
                "and create a new one."
            )

        try:
            self._seal(handle)
        except Exception as exc:  # noqa: BLE001
            return (
                note
                + " WARNING: its network could not be re-sealed after the "
                f"restart ({type(exc).__name__}: {exc}). Destroy this "
                "sandbox rather than running anything else in it."
            )
        return note

    def _explain_sigkill(self, handle: SandboxHandle) -> str:
        return sandbox_ops.explain_sigkill(lambda: self._container(handle))

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
        self._canary_verified.discard(handle.sandbox_id)
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
        registry. Matches on our labels only."""
        return sandbox_ops.collect_orphans(known_ids, _BACKENDS)
