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
import sys
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


WINDOWS = sys.platform == "win32"

# Podman ships outside PATH on macOS often enough that a "not installed"
# diagnosis would be wrong. These are the real install locations, checked
# only to produce an accurate message.
_PODMAN_BIN_CANDIDATES = (
    "/opt/podman/bin/podman",
    "/opt/homebrew/bin/podman",
    "/usr/local/bin/podman",
    # Windows installs, for the same reason.
    r"C:\Program Files\RedHat\Podman\podman.exe",
    r"C:\Program Files\Podman\podman.exe",
)

# Windows has no unix sockets; both engines are reached over named pipes.
# docker-py speaks npipe (docker/transport/npipeconn.py). podman-py does
# NOT — its only transports are unix, ssh and tcp — so Podman on Windows
# is reached through the Docker-compatible API it already serves, using
# the docker client against Podman's own pipe. Podman exists to be API
# compatible; this is that compatibility being used as intended.
_WINDOWS_DOCKER_PIPES = (r"npipe:////./pipe/docker_engine",)
_WINDOWS_PODMAN_PIPES = (
    r"npipe:////./pipe/podman-machine-default",
    r"npipe:////./pipe/podman",
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

    # POSIX-only: Windows has no unix sockets, and getuid does not exist.
    if not WINDOWS:
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
    if WINDOWS:
        # Nothing to choose: there is no unix socket to prefer, and the
        # named-pipe route goes through the docker client instead.
        return ""
    current = os.environ.get("CONTAINER_HOST", "")
    if current.startswith("unix://"):
        return current[len("unix://") :]
    socket_path = podman_socket()
    if socket_path:
        os.environ["CONTAINER_HOST"] = f"unix://{socket_path}"
        return socket_path
    return ""


def identify(engine_client: Any) -> str:
    """Which engine is actually answering — the product, not the pipe.

    Podman serves a Docker-compatible endpoint, so "something answered
    the docker pipe" says nothing about what is running. On a machine
    with Podman and no Docker installed at all, the old code reported
    "docker engine reachable, version 6.1.1", which is the same class of
    untruth as reporting a limit that was never applied.

    The API distinguishes them plainly, verified against both engines:

        docker  Components: ['Engine', 'containerd', 'runc', ...]
        podman  Components: ['Podman Engine', 'Conmon', 'OCI Runtime']

    Falls back to "docker" when the field is missing, because the
    Docker-shaped API is what we are speaking at that point.
    """
    try:
        raw = engine_client.version() or {}
    except Exception:  # noqa: BLE001 - identity is best effort
        return "docker"
    components = raw.get("Components") or []
    names = " ".join(str(c.get("Name", "")) for c in components).lower()
    blob = f"{names} {raw.get('Version', '')} {raw.get('Platform', '')}".lower()
    return "podman" if "podman" in blob else "docker"


def client_dialect(backend: str) -> str:
    """Which client library and API shape talks to this backend.

    This is the DIALECT, not the product, and the difference matters:
    Podman's Docker-compatible endpoint accepts Docker's HostConfig
    (`nano_cpus`, `tmpfs`), while podman-py's libpod API rejects both and
    wants `cpu_quota` and `mounts` instead. Container configuration must
    follow whichever API is being spoken.

    So this deliberately does NOT consult `identify()`. On Windows,
    Podman is reached through docker-py over its compatible pipe, and the
    correct config there is the Docker spelling even though the engine is
    Podman. Changing this to follow the product reintroduces silently
    unapplied limits.
    """
    if backend == "podman" and WINDOWS:
        return "docker"
    return backend



def _docker_client(base_url: str | None = None) -> Any:
    """A pinged docker-py client, optionally against an explicit URL."""
    import docker

    client = (
        docker.DockerClient(base_url=base_url) if base_url else docker.from_env()
    )
    client.ping()
    return client


def _windows_podman_pipes() -> list[str]:
    """Named pipes that might be serving Podman, best candidate first.

    Guessing from a fixed list was the bug: on a machine with Podman and
    no Docker, Podman answers `docker_engine` and nothing else, so an
    explicit backend="podman" failed while backend="auto" worked. Ask
    Podman where it actually listens, then fall back to the known names —
    including the Docker-compatible one, which is checked by identity
    rather than trusted by name.
    """
    candidates: list[str] = []
    binary = podman_binary()
    if binary:
        for args, prefix in (
            (["machine", "inspect", "--format",
              "{{.ConnectionInfo.PodmanPipe.Path}}"], ""),
            (["system", "connection", "list", "--format", "{{.URI}}"], ""),
        ):
            try:
                out = subprocess.run(
                    [binary, *args], capture_output=True, text=True, timeout=15
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode != 0:
                continue
            for line in out.stdout.splitlines():
                value = line.strip()
                if not value or value in ("<no value>", "<nil>"):
                    continue
                if not value.startswith("npipe://"):
                    value = f"npipe://{value}" if value.startswith("\\\\") else value
                if value.startswith("npipe://") and value not in candidates:
                    candidates.append(prefix + value)

    for pipe in (*_WINDOWS_PODMAN_PIPES, *_WINDOWS_DOCKER_PIPES):
        if pipe not in candidates:
            candidates.append(pipe)
    return candidates


def _build_windows_podman_client() -> Any:
    """Reach Podman on Windows, whichever pipe it happens to serve.

    The Docker-compatible pipe is a legitimate way to reach Podman, so it
    is tried — but only accepted if the engine on the other end really is
    Podman. Accepting it on name alone would hand back Docker when the
    caller asked for Podman.
    """
    tried: list[str] = []
    for pipe in _windows_podman_pipes():
        try:
            candidate = _docker_client(pipe)
        except Exception as exc:  # noqa: BLE001 - try the next pipe
            tried.append(f"{pipe} ({type(exc).__name__})")
            continue
        product = identify(candidate)
        if product == "podman":
            return candidate
        tried.append(f"{pipe} (answered by {product}, not podman)")
    raise EngineUnavailableError(
        "Podman is not reachable on any named pipe it advertises: "
        + ", ".join(tried)
        + ".",
        "Start the Podman machine: `podman machine start` (first time: "
        "`podman machine init`). Podman Desktop must be running, and its "
        "Docker-compatible endpoint enabled — that is the only transport "
        "the Python client can use on Windows.",
    )


def _build_client(backend: str) -> Any:
    """Construct a client and prove it can talk. A client object that
    constructs but cannot reach its engine is precisely the failure that
    produced empty output with exit code 0."""
    if backend == "podman" and WINDOWS:
        return _build_windows_podman_client()

    if backend == "docker":
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise EngineUnavailableError(
                "The docker client library is not installed.",
                "Reinstall the project: `uv sync`.",
            ) from exc
        try:
            return _docker_client()
        except Exception as exc:  # noqa: BLE001 - any failure here is "unreachable"
            raise EngineUnavailableError(
                f"Docker is not reachable ({type(exc).__name__}: {exc}).",
                _DOCKER_FIX,
            ) from exc

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

#: Substrings of the errors a dead-but-cached connection produces.
#:
#: Engines close idle connections. Docker Desktop on Windows does it
#: within seconds on its named pipe, and a restarted engine drops unix
#: sockets the same way. The cached client keeps the dead handle, so the
#: NEXT call fails while the engine is perfectly healthy — which is how a
#: destroy() came to report an unreachable engine and leave a container
#: running.
#:
#: The Windows phrasings are here because a real run hit them: a named
#: pipe reports "The pipe is being closed" (WinError 232) or "The pipe
#: has been ended" (109) rather than anything resembling a socket error.
_STALE_CONNECTION_MARKERS = (
    "connection aborted",
    "remote end closed connection",
    "remotedisconnected",
    "connection reset",
    "broken pipe",
    "pipe is being closed",
    "pipe has been ended",
    "no process is on the other end",
    "winerror 232",
    "winerror 109",
    "cannot connect to host",
)


def is_stale_connection(exc: BaseException) -> bool:
    """Whether `exc` looks like a dropped connection rather than an outage.

    The distinction is the whole point: a dropped connection is worth
    retrying against a fresh client, an unreachable engine is not, and
    conflating them would let a real outage be retried into a false
    success. The registry's correctness rests on that line.
    """
    text = f"{exc} {getattr(exc, '__cause__', '')}".lower()
    return any(marker in text for marker in _STALE_CONNECTION_MARKERS)


def with_retry(operation, what: str = ""):
    """Run `operation`, once more against fresh clients if the connection
    was merely stale.

    Every engine call goes through here rather than each caller
    remembering to handle it. Sealing a network, listing our containers
    and garbage collection are all as exposed to an idle pipe as looking
    up a container is — a lesson learned by protecting exactly one of
    them and watching a benchmark die on the others.
    """
    try:
        return operation()
    except EngineUnavailableError as exc:
        if not is_stale_connection(exc):
            raise
        reset_clients()
    except Exception as exc:  # noqa: BLE001 - classified by the caller
        if not is_stale_connection(exc):
            raise
        reset_clients()
    # One retry, on a client rebuilt from scratch. A second failure is
    # reported as-is: an engine that drops two fresh connections is not
    # having a transient problem.
    return operation()


def client(backend: str) -> Any:
    """A live, verified client for `backend`.

    Cached after the first successful ping so routine operations do not
    pay a round trip each.
    """
    existing = _clients.get(backend)
    if existing is not None:
        return existing
    built = _build_client(backend)
    _clients[backend] = built
    return built


def reset_clients() -> None:
    """Drop cached clients, so the next call builds a fresh connection."""
    _clients.clear()


def _not_found_types(backend: str) -> tuple[type[BaseException], ...]:
    """The exception types that mean, authoritatively, 'no such thing'."""
    types: list[type[BaseException]] = []
    backend = client_dialect(backend)
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


def session_kwargs(backend: str) -> dict:
    """Extra arguments the execution backend needs for this engine.

    On Windows a Podman sandbox runs through llm-sandbox's Docker session
    class with a client pointed at Podman's pipe, because that is the
    only transport available. Everywhere else the backend speaks for
    itself and this is empty.
    """
    if backend == "podman" and WINDOWS:
        return {"session_backend": "docker", "client": client("podman")}
    return {}


def get_container(backend: str, ref: str) -> Any:
    """Fetch a container by reference.

    Raises ContainerGoneError only when the engine said so, and
    EngineUnavailableError whenever it could not be asked.
    """
    def op():
        engine = client(backend)  # may raise EngineUnavailableError
        try:
            return engine.containers.get(ref)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            raise classify(backend, exc, f"container {ref[:12]}") from exc

    return with_retry(op, f"container {ref[:12]}")


def list_managed(backend: str, label: str) -> list[Any]:
    """Every container carrying `label`. Raises if the engine is down —
    an empty list must mean 'none', never 'could not ask'."""
    def op():
        engine = client(backend)
        try:
            return list(engine.containers.list(all=True, filters={"label": label}))
        except Exception as exc:  # noqa: BLE001
            raise classify(backend, exc, "the managed container list") from exc

    return with_retry(op, "the managed container list")


def image_present(backend: str, image: str) -> bool:
    """Whether `image` is already on this engine.

    Used to decide whether creating a sandbox can finish inside an MCP
    client's request timeout. Raises if the engine cannot be reached, so
    "absent" never silently means "could not ask".
    """
    def op():
        engine_client = client(backend)
        try:
            engine_client.images.get(image)
            return True
        except Exception as exc:  # noqa: BLE001
            if is_not_found(backend, exc):
                return False
            raise classify(backend, exc, f"image {image}") from exc

    return with_retry(op, f"image {image}")


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
    # Which engine actually answered. Worth stating whenever it differs
    # from what was asked for, because that is exactly the case a person
    # would otherwise misread.
    product = identify(engine)
    status.extra["product"] = product
    if product != backend:
        status.extra["note"] = (
            f"this endpoint is served by {product}, not {backend}"
        )
    if backend == "podman":
        status.extra["machine"] = _podman_machine_state(binary) or "not reported"
        if WINDOWS:
            status.extra["transport"] = (
                "named pipe via Podman's Docker-compatible API — the only "
                "transport the Python client supports on Windows"
            )
            return status
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
        engine_client = client(preferred)  # raises, with a fix
        product = identify(engine_client)
        if product != preferred:
            # Asking for Docker and being handed Podman is not a
            # successful resolution, it is a wrong answer that would then
            # be reported under the wrong name for the sandbox's whole
            # life.
            raise EngineUnavailableError(
                f"'{preferred}' was requested, but the engine answering is "
                f"{product}. Use backend='{product}', or 'auto'."
            )
        return preferred

    problems: list[str] = []
    for backend in BACKENDS:
        try:
            engine_client = client(backend)
        except EngineUnavailableError as exc:
            problems.append(f"  {backend}: {exc}")
            continue
        # Report what is actually running, not which client reached it.
        # Podman commonly answers the Docker pipe; calling that "docker"
        # misleads every later message about the sandbox.
        product = identify(engine_client)
        if product != backend:
            _clients.setdefault(product, engine_client)
        return product
    raise EngineUnavailableError(
        "No container engine is reachable, so there is nowhere to run code.\n"
        + "\n".join(problems)
    )
