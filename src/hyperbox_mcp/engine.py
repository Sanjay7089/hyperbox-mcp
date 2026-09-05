"""The only module that talks to a container engine client directly.

Its job is to tell the truth about the engine. Everything else in
HyperBox depends on being able to distinguish these two situations,
which the rest of the codebase used to collapse into one bare
`except Exception`:

    the engine answered, and the container is not there   -> ContainerGoneError
    the engine could not be asked at all                  -> EngineUnavailableError

That distinction is the difference between "your sandbox is cleaned up"
and "your sandbox may still be running and I have no idea". Reporting
the second as the first is how orphaned containers accumulate, so no
function here may swallow a connection failure.

This module imports docker/podman clients. It does NOT import
llm_sandbox — that stays confined to llm_sandbox_runtime.py, which is
what keeps the execution backend replaceable.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from hyperbox_mcp.policy import BACKENDS


class EngineUnavailableError(RuntimeError):
    """The container engine could not be reached at all.

    Carries a `fix` a developer can act on. Never raised to mean "the
    container is gone" — that is ContainerGoneError.
    """

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message if not fix else f"{message} {fix}")
        self.fix = fix


class ContainerGoneError(LookupError):
    """The engine answered and confirmed this container does not exist."""


class UnsupportedBackendError(ValueError):
    pass


# Podman ships outside PATH on macOS often enough that a "not installed"
# diagnosis would be wrong. These are the real install locations, checked
# only to produce an accurate message.
_PODMAN_BIN_CANDIDATES = (
    "/opt/podman/bin/podman",
    "/opt/homebrew/bin/podman",
    "/usr/local/bin/podman",
)

_DOCKER_FIX = (
    "Start the engine: open Docker Desktop, or `sudo systemctl start docker`. "
    "Verify with `docker info`."
)

_PODMAN_MACHINE_FIX = (
    "Start the Podman VM: `podman machine start` (first time: "
    "`podman machine init`). If the socket is still not found, export it "
    "explicitly:\n"
    "  export CONTAINER_HOST=\"unix://$(podman machine inspect "
    "--format '{{.ConnectionInfo.PodmanSocket.Path}}')\""
)

# Rootless socket locations to try when there is no Podman VM (Linux).
_PODMAN_NATIVE_SOCKETS = (
    "/run/user/{uid}/podman/podman.sock",
    "/run/podman/podman.sock",
)

# macOS gives each user a private temp directory and Podman puts its
# machine socket inside it. Podman derives that location from $TMPDIR, so
# a process launched without TMPDIR — which is how MCP clients launch
# their servers — is told the socket is at /tmp/podman/... when it is
# really under /var/folders. Globbing finds it either way.
_PODMAN_SOCKET_GLOBS = (
    "/var/folders/*/*/T/podman/*-api.sock",
    "/tmp/podman/*-api.sock",
)


@dataclass
class EngineStatus:
    """What `probe` learned about one engine. Every field is observed,
    never assumed — `reachable` means an API call actually returned."""

    backend: str
    reachable: bool
    version: str = ""
    detail: str = ""
    fix: str = ""
    binary: str = ""
    extra: dict = field(default_factory=dict)


def podman_binary() -> str:
    """Path to the podman CLI, PATH first then the known install dirs.

    Only used for diagnostics. The API connection never goes through the
    CLI, so a podman that is reachable over its socket but absent from
    PATH is fully usable — a case this machine actually exhibits.
    """
    found = shutil.which("podman")
    if found:
        return found
    for candidate in _PODMAN_BIN_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return ""


def _podman_machine_state(binary: str) -> str:
    """Ask the podman CLI whether a VM is running. Best effort: this is
    context for an error message, never a gate on anything."""
    if not binary:
        return ""
    try:
        out = subprocess.run(
            [binary, "machine", "list", "--format", "{{.Name}} {{.LastUp}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def podman_socket(binary: str = "") -> str:
    """The unix socket path for the local Podman service, if there is one.

    Asks the CLI where its machine put the socket, then falls back to the
    rootless locations used when Podman runs natively.
    """
    reported = ""
    binary = binary or podman_binary()
    if binary:
        try:
            out = subprocess.run(
                [
                    binary,
                    "machine",
                    "inspect",
                    "--format",
                    "{{.ConnectionInfo.PodmanSocket.Path}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            reported = out.stdout.strip().splitlines()[0].strip()

    if reported and os.path.exists(reported):
        return reported

    # The reported path can be wrong without TMPDIR, but its basename
    # still names the right machine, so prefer a glob hit that matches it.
    wanted = os.path.basename(reported) if reported else ""
    fallbacks: list[str] = []
    for pattern in _PODMAN_SOCKET_GLOBS:
        fallbacks.extend(sorted(glob.glob(pattern)))
    for candidate in fallbacks:
        if wanted and os.path.basename(candidate) == wanted:
            return candidate
    if fallbacks:
        return fallbacks[0]

    for template in _PODMAN_NATIVE_SOCKETS:
        candidate = template.format(uid=os.getuid())
        if os.path.exists(candidate):
            return candidate
    return ""


def ensure_podman_transport() -> str:
    """Point podman-py at the unix socket rather than a TCP forward.

    This is not a preference, it is a correctness fix. `PodmanClient.from_env()`
    will happily pick the TCP port that `podman machine` forwards, and over
    that forward Podman answers every request EXCEPT the one that matters:
    the hijacked exec stream comes back with zero bytes. Containers start,
    exit codes are correct, and every command appears to succeed while
    producing no output whatsoever.

    Measured on Podman 6.1.1 / API 1.44: over the TCP forward an exec
    returns `b''` with the right exit code; over the unix socket the same
    exec returns a correctly framed `\x01...` stdout stream.

    Setting CONTAINER_HOST in this process's environment fixes it for
    every podman client created afterwards, including the ones the
    execution backend builds internally.

    Returns the socket in use, or "" if none could be found. An explicit
    unix:// CONTAINER_HOST from the user is always left alone.
    """
    current = os.environ.get("CONTAINER_HOST", "")
    if current.startswith("unix://"):
        return current[len("unix://") :]
    socket_path = podman_socket()
    if socket_path:
        os.environ["CONTAINER_HOST"] = f"unix://{socket_path}"
        return socket_path
    return ""


def _build_client(backend: str) -> Any:
    """Construct a client and prove it can talk. A client object that
    constructs but cannot reach its engine is precisely the failure that
    produced empty output with exit code 0."""
    if backend == "docker":
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise EngineUnavailableError(
                "The docker client library is not installed.",
                "Reinstall the project: `uv sync`.",
            ) from exc
        try:
            client = docker.from_env()
            client.ping()
        except Exception as exc:  # noqa: BLE001 - any failure here is "unreachable"
            raise EngineUnavailableError(
                f"Docker is not reachable ({type(exc).__name__}: {exc}).",
                _DOCKER_FIX,
            ) from exc
        return client

    if backend == "podman":
        try:
            from podman import PodmanClient
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise EngineUnavailableError(
                "The podman client library is not installed.",
                "Reinstall the project: `uv sync`.",
            ) from exc
        # Must happen before the client is built: over a TCP forward,
        # exec output never arrives. See ensure_podman_transport.
        ensure_podman_transport()
        try:
            client = PodmanClient.from_env()
            client.ping()
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailableError(
                f"Podman is not reachable ({type(exc).__name__}: {exc}).",
                _PODMAN_MACHINE_FIX,
            ) from exc
        return client

    raise UnsupportedBackendError(
        f"Unsupported backend '{backend}'. Supported: {', '.join(BACKENDS)}"
    )


_clients: dict[str, Any] = {}


def client(backend: str) -> Any:
    """A live, verified client for `backend`.

    Cached after the first successful ping so routine operations do not
    pay a round trip each. A cached client whose engine later dies still
    fails correctly: the operation raises, and `classify` maps it to
    EngineUnavailableError.
    """
    existing = _clients.get(backend)
    if existing is not None:
        return existing
    built = _build_client(backend)
    _clients[backend] = built
    return built


def reset_clients() -> None:
    """Drop cached clients. For tests that change engine availability."""
    _clients.clear()


def _not_found_types(backend: str) -> tuple[type[BaseException], ...]:
    """The exception types that mean, authoritatively, 'no such thing'."""
    types: list[type[BaseException]] = []
    if backend == "docker":
        try:
            from docker.errors import ImageNotFound, NotFound

            types += [NotFound, ImageNotFound]
        except ImportError:  # pragma: no cover
            pass
    elif backend == "podman":
        try:
            from podman.errors import NotFound as PodmanNotFound

            types.append(PodmanNotFound)
        except ImportError:  # pragma: no cover
            pass
        try:
            from podman.errors.exceptions import ImageNotFound

            types.append(ImageNotFound)
        except ImportError:  # pragma: no cover
            pass
    return tuple(types)


def is_not_found(backend: str, exc: BaseException) -> bool:
    """Whether `exc` is the engine positively confirming absence.

    A 404 from a live engine is an answer. A refused connection is not —
    it must never be read as absence.
    """
    types = _not_found_types(backend)
    return bool(types) and isinstance(exc, types)


def classify(backend: str, exc: BaseException, what: str) -> BaseException:
    """Turn a raw client exception into one of our two truths."""
    if is_not_found(backend, exc):
        return ContainerGoneError(f"{what} does not exist on {backend}.")
    fix = _DOCKER_FIX if backend == "docker" else _PODMAN_MACHINE_FIX
    return EngineUnavailableError(
        f"Could not reach {backend} while looking up {what} "
        f"({type(exc).__name__}: {exc}).",
        fix,
    )


def get_container(backend: str, ref: str) -> Any:
    """Fetch a container by reference.

    Raises ContainerGoneError only when the engine said so, and
    EngineUnavailableError whenever it could not be asked.
    """
    engine = client(backend)  # may raise EngineUnavailableError
    try:
        return engine.containers.get(ref)
    except Exception as exc:  # noqa: BLE001 - classified immediately below
        raise classify(backend, exc, f"container {ref[:12]}") from exc


def list_managed(backend: str, label: str) -> list[Any]:
    """Every container carrying `label`. Raises if the engine is down —
    an empty list must mean 'none', never 'could not ask'."""
    engine = client(backend)
    try:
        return list(engine.containers.list(all=True, filters={"label": label}))
    except Exception as exc:  # noqa: BLE001
        raise classify(backend, exc, "the managed container list") from exc


def probe(backend: str) -> EngineStatus:
    """Diagnose one engine without raising. Backs `hyperbox doctor`."""
    binary = podman_binary() if backend == "podman" else (shutil.which("docker") or "")
    try:
        engine = client(backend)
    except EngineUnavailableError as exc:
        status = EngineStatus(
            backend=backend,
            reachable=False,
            detail=str(exc).split(" Start")[0].strip(),
            fix=exc.fix,
            binary=binary,
        )
        if backend == "podman":
            machine = _podman_machine_state(binary)
            status.extra["machine"] = machine or "no VM reported"
            if not binary:
                status.detail = (
                    "The podman CLI was not found on PATH or in the usual "
                    "install locations, and its socket is unreachable."
                )
        return status
    except UnsupportedBackendError as exc:
        return EngineStatus(backend=backend, reachable=False, detail=str(exc))

    version = ""
    try:
        raw = engine.version()
        version = str(raw.get("Version", "") or raw.get("ApiVersion", ""))
    except Exception as exc:  # noqa: BLE001 - version is informational
        version = f"unknown ({type(exc).__name__})"

    status = EngineStatus(
        backend=backend, reachable=True, version=version, binary=binary
    )
    if backend == "podman":
        status.extra["machine"] = _podman_machine_state(binary) or "not reported"
        host = os.environ.get("CONTAINER_HOST", "")
        status.extra["CONTAINER_HOST"] = host or "unset"
        if host.startswith("unix://"):
            status.extra["transport"] = "unix socket (exec output works)"
        else:
            status.extra["transport"] = (
                "NOT a unix socket — exec output will be empty over a TCP "
                "forward. Export CONTAINER_HOST as shown below."
            )
            status.fix = _PODMAN_MACHINE_FIX
    return status


def detect(preferred: str = "auto") -> str:
    """Resolve a backend choice to an engine that is reachable right now.

    "auto" prefers docker and falls back to podman. A named backend is
    still verified rather than assumed, so a caller never receives a
    sandbox id for an engine that cannot run it.
    """
    if preferred != "auto":
        if preferred not in BACKENDS:
            raise UnsupportedBackendError(
                f"Unsupported backend '{preferred}'. Supported: "
                f"auto, {', '.join(BACKENDS)}"
            )
        client(preferred)  # raises EngineUnavailableError with a fix
        return preferred

    problems: list[str] = []
    for backend in BACKENDS:
        try:
            client(backend)
            return backend
        except EngineUnavailableError as exc:
            problems.append(f"  {backend}: {exc}")
    raise EngineUnavailableError(
        "No container engine is reachable, so there is nowhere to run code.\n"
        + "\n".join(problems)
    )
