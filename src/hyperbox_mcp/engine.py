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
import http.client
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from hyperbox_mcp import errors
from hyperbox_mcp.policy import BACKENDS


# Defined in errors.py, re-exported here so every existing import keeps
# working. They carry a machine-readable code and a `fix` now; the
# behaviour, including which builtin each subclasses, is unchanged.
#
# The rule this module exists to enforce is unchanged too:
# EngineUnavailableError means the engine could not be ASKED,
# ContainerGoneError means it answered and said no. Neither subclasses the
# other, so no handler can quietly treat them alike.
EngineUnavailableError = errors.EngineUnavailableError
ContainerGoneError = errors.ContainerGoneError
UnsupportedBackendError = errors.UnsupportedBackendError
NoEngineError = errors.NoEngineError


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

#: How long to wait for a socket to accept a connection before calling it
#: dead.
#:
#: Existence is NOT liveness, and conflating them is what produced
#: "connection refused to podman" on a machine whose Podman was fine. A
#: stopped `podman machine` leaves its `*-api.sock` file on disk; connecting
#: to it gives ECONNREFUSED rather than ENOENT.
#:
#: Kept short deliberately. This runs once per candidate on the
#: create_sandbox path, so at one second apiece a handful of dead
#: candidates would add seconds to every sandbox creation. A unix socket
#: that has not accepted in 200ms is not going to.
SOCKET_PROBE_TIMEOUT = 0.2

#: How long to let the podman CLI answer. The full timeout is for paths
#: where a person is waiting; the short one is for background work
#: (garbage collection) where a stopped engine must not stall a sweep.
PODMAN_CLI_TIMEOUT = 15.0
PODMAN_CLI_TIMEOUT_FAST = 2.0

#: How long a failed resolution is remembered, so a stopped engine is not
#: re-probed on every sweep.
PROBE_FAILURE_TTL = 60.0


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


def _podman_machine_state(
    binary: str, cli_timeout: float = PODMAN_CLI_TIMEOUT
) -> str:
    """Ask the podman CLI whether a VM is running. Best effort: this is
    context for an error message, never a gate on anything."""
    if not binary:
        return ""
    try:
        out = subprocess.run(
            [binary, "machine", "list", "--format", "{{.Name}} {{.LastUp}}"],
            capture_output=True,
            text=True,
            timeout=cli_timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _socket_is_live(path: str) -> bool:
    """Whether anything is actually LISTENING on this unix socket.

    Existence is not the test, and treating it as one is the bug this
    function exists to close. A `podman machine stop` leaves its
    `*-api.sock` file on disk; so does a reboot, and so does a machine
    recreated under a new path. Connecting to such a file gives
    ECONNREFUSED — not ENOENT — which is precisely the "connection refused
    to podman" a healthy machine was reported as.

    Never raises: an unprobeable candidate is simply not a candidate.
    """
    if WINDOWS or not path:
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(SOCKET_PROBE_TIMEOUT)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _cli_reported_socket(binary: str, cli_timeout: float) -> str:
    """Where the podman CLI says its machine put the socket.

    Only consulted when the cheap paths found nothing live, because it
    costs a subprocess: on a stopped machine that is the single slowest
    thing in engine discovery, and it used to run on every sandbox
    creation and every garbage-collection sweep.
    """
    if not binary:
        return ""
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
            timeout=cli_timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        return ""
    return out.stdout.strip().splitlines()[0].strip()


def _glob_candidates() -> list[str]:
    """Socket paths the machine has left lying around, newest first.

    Newest first because when several are present the survivor of the most
    recent `podman machine` run is overwhelmingly the live one; liveness is
    still checked, this only decides what to check first.
    """
    found: list[str] = []
    for pattern in _PODMAN_SOCKET_GLOBS:
        found.extend(glob.glob(pattern))
    unique = list(dict.fromkeys(found))

    def age(path: str) -> float:
        try:
            return -os.stat(path).st_mtime
        except OSError:
            return 0.0

    return sorted(unique, key=age)


def podman_socket(
    binary: str = "", cli_timeout: float = PODMAN_CLI_TIMEOUT
) -> str:
    """A unix socket for the local Podman service that is ACCEPTING now.

    Returns "" when nothing is listening, so a caller reports "no Podman
    socket is accepting connections" instead of handing back a path that
    is guaranteed to fail with ECONNREFUSED further down.

    Cheap candidates are tried first and the podman CLI is consulted only
    if none of them are live. That ordering is not a micro-optimisation:
    the CLI call is a subprocess with a multi-second timeout, it used to
    run on every sandbox creation, and once liveness is the test it has
    nothing left to contribute on the happy path — any live socket is
    usable, whichever machine put it there.
    """
    for candidate in _glob_candidates():
        if _socket_is_live(candidate):
            return candidate

    # POSIX-only: Windows has no unix sockets, and getuid does not exist.
    if not WINDOWS:
        for template in _PODMAN_NATIVE_SOCKETS:
            candidate = template.format(uid=os.getuid())
            if _socket_is_live(candidate):
                return candidate

    reported = _cli_reported_socket(binary or podman_binary(), cli_timeout)
    if _socket_is_live(reported):
        return reported

    # The reported path can be wrong without TMPDIR, but its basename still
    # names the right machine. Nothing globbed was live, so this is a last
    # look rather than a likely hit.
    wanted = os.path.basename(reported) if reported else ""
    if wanted:
        for candidate in _glob_candidates():
            if os.path.basename(candidate) == wanted and _socket_is_live(
                candidate
            ):
                return candidate
    return ""


#: The CONTAINER_HOST value this process set for itself, if any.
#:
#: Tracked so `reset_podman_transport` can drop OUR resolution without
#: discarding one the user set deliberately. A user pointing at a remote or
#: unusual endpoint has made a choice; silently overwriting it would be the
#: same class of confident wrongness this module exists to avoid.
_own_container_host: str = ""


def ensure_podman_transport(
    cli_timeout: float = PODMAN_CLI_TIMEOUT,
) -> str:
    """Point podman-py at a unix socket that is alive right now.

    This is not a preference, it is a correctness fix. `PodmanClient.from_env()`
    will happily pick the TCP port that `podman machine` forwards, and over
    that forward Podman answers every request EXCEPT the one that matters:
    the hijacked exec stream comes back with zero bytes. Containers start,
    exit codes are correct, and every command appears to succeed while
    producing no output whatsoever.

    Measured on Podman 6.1.1 / API 1.44: over the TCP forward an exec
    returns `b''` with the right exit code; over the unix socket the same
    exec returns a correctly framed `\x01...` stdout stream.

    An existing `unix://` value is RE-VALIDATED rather than trusted. It used
    to be returned untouched, which meant a process that resolved a socket
    once kept it forever — and a server inside an editor runs for days. Stop
    the podman machine and the socket file stays on disk, so every later call
    failed with ECONNREFUSED against a path that could never work again, on a
    machine whose Podman was restarted and perfectly healthy.

    A user-supplied value that is dead is replaced only if a live socket is
    found; otherwise it is put back, because refusing with their own setting
    intact is more useful than refusing with it silently erased.

    Returns the socket in use, or "" if none could be found.
    """
    global _own_container_host

    if WINDOWS:
        # Nothing to choose: there is no unix socket to prefer, and the
        # named-pipe route goes through the docker client instead.
        return ""

    current = os.environ.get("CONTAINER_HOST", "")
    if current.startswith("unix://"):
        existing = current[len("unix://") :]
        if _socket_is_live(existing):
            return existing
        if current != _own_container_host:
            # Somebody chose this endpoint deliberately — a client config,
            # an exported variable, a deployment. It is dead, and the
            # honest answer is that it is dead. Quietly resolving a
            # DIFFERENT endpoint and using that is the same wrong answer as
            # handing back Podman to a caller who asked for Docker: the
            # operation would succeed against something they did not name.
            return ""
        os.environ.pop("CONTAINER_HOST", None)

    socket_path = podman_socket(cli_timeout=cli_timeout)
    if socket_path:
        _own_container_host = f"unix://{socket_path}"
        os.environ["CONTAINER_HOST"] = _own_container_host
        return socket_path

    if current and not current.startswith("unix://"):
        # Not ours to resolve — a TCP or ssh endpoint the user chose.
        return ""
    if current:
        os.environ["CONTAINER_HOST"] = current
    return ""


def reset_podman_transport() -> None:
    """Forget our resolved Podman socket so the next client re-resolves.

    Called from `reset_clients`, and the pairing is load-bearing: dropping
    the cached clients while leaving CONTAINER_HOST pointing at a dead
    socket rebuilds a client against the same dead path, so the retry is
    guaranteed to fail for exactly the reason the first attempt did. That
    is what turns one stopped machine into a permanently broken process.

    Only clears a value this process set. A user's own CONTAINER_HOST is
    left alone.
    """
    global _own_container_host
    if _own_container_host and os.environ.get("CONTAINER_HOST") == _own_container_host:
        os.environ.pop("CONTAINER_HOST", None)
    _own_container_host = ""


#: Where Docker's socket turns up. Docker Desktop creates several and the
#: active context decides which one is authoritative, so this is a list to
#: probe, not a guess to trust.
_DOCKER_SOCKET_CANDIDATES = (
    "~/.docker/run/docker.sock",
    "/var/run/docker.sock",
    "/run/docker.sock",
)


def _docker_context_endpoint() -> str:
    """What `docker context` says the active endpoint is.

    Consulted, not obeyed: it names a socket that may not be listening, and
    the CLI may not be installed at all. The answer is liveness-tested like
    every other candidate.
    """
    binary = shutil.which("docker")
    if not binary:
        return ""
    try:
        out = subprocess.run(
            [binary, "context", "inspect", "--format",
             "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True, timeout=PODMAN_CLI_TIMEOUT_FAST,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    value = out.stdout.strip() if out.returncode == 0 else ""
    return value[len("unix://"):] if value.startswith("unix://") else ""


def docker_socket() -> str:
    """A Docker socket that is accepting connections, or "".

    DOCKER_HOST wins outright when set: it is a deliberate choice, and
    quietly using a different endpoint than the one someone named is the
    same wrong answer as handing back Podman to a caller who asked for
    Docker.
    """
    configured = os.environ.get("DOCKER_HOST", "").strip()
    if configured:
        return configured

    candidates = []
    reported = _docker_context_endpoint()
    if reported:
        candidates.append(reported)
    candidates.extend(os.path.expanduser(p) for p in _DOCKER_SOCKET_CANDIDATES)
    for candidate in candidates:
        if _socket_is_live(candidate):
            return candidate
    return ""


def endpoint_for(backend: str) -> str:
    """The address the REST driver should dial for `backend`.

    A path or URL rather than a client object: the REST driver builds its
    own connections, and the transport it needs is decided by the address
    it is given. Raises rather than returning an empty string, because
    "nowhere to connect" is a diagnosis a caller should not have to infer.
    """
    if backend not in BACKENDS:
        raise UnsupportedBackendError(
            f"Unsupported backend '{backend}'. Supported: {', '.join(BACKENDS)}"
        )
    if WINDOWS:
        # Podman has no named-pipe client of its own, so both engines are
        # reached over pipes here; which pipe answers is discovered, and
        # the product is confirmed by identify() afterwards rather than
        # assumed from the name.
        pipes = _windows_podman_pipes() if backend == "podman" else list(
            _WINDOWS_DOCKER_PIPES
        )
        if not pipes:
            raise EngineUnavailableError(
                f"No named pipe found for {backend}.", _DOCKER_FIX
            )
        return pipes[0]

    if backend == "podman":
        socket_path = ensure_podman_transport()
        if socket_path:
            return socket_path
        configured = os.environ.get("CONTAINER_HOST", "")
        if configured:
            return configured
        raise EngineUnavailableError(
            "No Podman socket is accepting connections.", _PODMAN_MACHINE_FIX
        )

    found = docker_socket()
    if not found:
        raise EngineUnavailableError("Docker is not reachable.", _DOCKER_FIX)
    return found


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

    # An explicit npipe:// endpoint wins outright. Two reasons, and the
    # second is not a test convenience:
    #
    #   - Someone running a non-default Podman machine, or Podman Desktop
    #     on a custom pipe, has no other way to say so on Windows. Every
    #     other platform honours CONTAINER_HOST; this one silently did not.
    #   - The acceptance suite makes an engine genuinely unreachable by
    #     pointing a server at an address where nothing listens. On Windows
    #     that had no effect at all, because this function ignored both
    #     variables and went looking for the real pipe -- so the "outage"
    #     server connected to a healthy engine and destroyed the container
    #     the test was asserting had survived.
    for variable in ("CONTAINER_HOST", "DOCKER_HOST"):
        value = os.environ.get(variable, "").strip()
        if value.startswith("npipe://"):
            return [value]
        if value:
            # Set, but not to a pipe. On Windows there is no other
            # transport, so honour the intent -- an endpoint that cannot be
            # reached is the honest answer, not a fallback to a different
            # engine than the one named.
            return [value]

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


def _build_client(backend: str, cli_timeout: float = PODMAN_CLI_TIMEOUT) -> Any:
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
        endpoint = os.environ.get("DOCKER_HOST", "")
        if _probe_failed_recently("docker", endpoint):
            raise EngineUnavailableError(
                f"Docker is not reachable (checked in the last "
                f"{PROBE_FAILURE_TTL:.0f}s).",
                _DOCKER_FIX,
            )
        try:
            return _docker_client()
        except Exception as exc:  # noqa: BLE001 - any failure here is "unreachable"
            _record_probe_failure("docker", endpoint)
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
        configured = os.environ.get("CONTAINER_HOST", "")
        if configured.startswith("unix://") and not _socket_is_live(
            configured[len("unix://") :]
        ):
            raise EngineUnavailableError(
                f"CONTAINER_HOST is set to {configured}, and nothing is "
                "listening there.",
                "Start that Podman service, or unset CONTAINER_HOST to let "
                "HyperBox find a live socket itself.",
            )
        if not configured and _probe_failed_recently("podman", ""):
            raise EngineUnavailableError(
                "No Podman socket is accepting connections (checked in the "
                f"last {PROBE_FAILURE_TTL:.0f}s).",
                _PODMAN_MACHINE_FIX,
            )

        # Must happen before the client is built: over a TCP forward,
        # exec output never arrives. See ensure_podman_transport.
        socket_path = ensure_podman_transport(cli_timeout=cli_timeout)
        if not socket_path and not os.environ.get("CONTAINER_HOST"):
            _record_probe_failure("podman", "")
            raise EngineUnavailableError(
                "No Podman socket is accepting connections. A stopped "
                "machine leaves its socket file on disk, so a socket file "
                "existing is not evidence that Podman is running.",
                _PODMAN_MACHINE_FIX,
            )
        try:
            client = PodmanClient.from_env()
            client.ping()
        except Exception as exc:  # noqa: BLE001
            # File it under the endpoint actually tried. The "" key means
            # "nothing could be resolved"; a user's own CONTAINER_HOST
            # failing is a different fact and filing it under "" would make
            # the next call claim no socket is accepting connections about
            # an endpoint that was never a socket.
            _record_probe_failure(
                "podman", socket_path or os.environ.get("CONTAINER_HOST", "")
            )
            raise EngineUnavailableError(
                f"Podman is not reachable ({type(exc).__name__}: {exc}).",
                _PODMAN_MACHINE_FIX,
            ) from exc
        return client

    raise UnsupportedBackendError(
        f"Unsupported backend '{backend}'. Supported: {', '.join(BACKENDS)}"
    )


_clients: dict[str, Any] = {}

#: Recent failed resolutions, so a stopped engine is not re-probed on every
#: call.
#:
#: Keyed on (backend, endpoint) rather than on backend alone. A machine can
#: serve two Podman sockets — a default machine and a named one — and one of
#: them being dead says nothing whatsoever about the other. Keying on the
#: backend would let the first dead socket mark the whole engine
#: unreachable, which is the same collapse of "this endpoint" into "this
#: engine" that the rest of this module exists to prevent.
_probe_failures: dict[tuple[str, str], float] = {}


def _probe_failed_recently(backend: str, endpoint: str) -> bool:
    stamp = _probe_failures.get((backend, endpoint))
    return stamp is not None and (time.monotonic() - stamp) < PROBE_FAILURE_TTL


def _record_probe_failure(backend: str, endpoint: str) -> None:
    _probe_failures[(backend, endpoint)] = time.monotonic()

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
    # Safe to retry ONLY because reset_clients now also drops the resolved
    # transport, so the retry re-resolves the socket instead of reconnecting
    # to the same dead path. Without that pairing this marker would turn one
    # stopped machine into two guaranteed failures instead of one.
    "connection refused",
)


#: Connection-shaped failures, by TYPE.
#:
#: The REST driver raises stdlib exceptions directly, so this is the
#: reliable half: ConnectionError covers refused, reset, aborted and broken
#: pipe, and http.client.RemoteDisconnected already subclasses
#: ConnectionResetError. Naming it anyway is documentation — it is the one
#: an idle engine actually produces.
#:
#: TimeoutError is deliberately absent. A hung engine retried is two long
#: waits instead of one, and the caller learns nothing new the second time.
_STALE_CONNECTION_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
)


def is_stale_connection(exc: BaseException) -> bool:
    """Whether `exc` looks like a dropped connection rather than an outage.

    The distinction is the whole point: a dropped connection is worth
    retrying against a fresh client, an unreachable engine is not, and
    conflating them would let a real outage be retried into a false
    success. The registry's correctness rests on that line.

    Two mechanisms, and the second is on its way out. Types are checked
    first because they cannot drift — an exception either is a
    ConnectionError or is not. The string markers below remain only for the
    SDK path: docker-py and podman-py wrap failures in their own exception
    classes and the shape survives solely in the message text, which is
    also why several of the markers are phrasings from requests and pywin32
    that stdlib will never produce. When those dependencies go, so does the
    string half.
    """
    if isinstance(exc, _STALE_CONNECTION_TYPES):
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and isinstance(cause, _STALE_CONNECTION_TYPES):
        return True
    text = f"{exc} {cause or ''}".lower()
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


def client(backend: str, cli_timeout: float = PODMAN_CLI_TIMEOUT) -> Any:
    """A live, verified client for `backend`.

    Cached after the first successful ping so routine operations do not
    pay a round trip each.

    `cli_timeout` bounds the podman CLI call that resolution may fall back
    to. Background callers pass PODMAN_CLI_TIMEOUT_FAST: garbage collection
    runs on a timer and must not stall for the full interactive budget just
    because an engine is stopped. It only matters on the failing path — a
    reachable engine never reaches the CLI at all.
    """
    existing = _clients.get(backend)
    if existing is not None:
        return existing
    built = _build_client(backend, cli_timeout=cli_timeout)
    _clients[backend] = built
    return built


def reset_clients() -> None:
    """Drop cached clients, the resolved Podman transport, and remembered
    failures, so the next call builds a fresh connection against a freshly
    resolved endpoint.

    Dropping the transport alongside the clients is load-bearing, not
    tidiness: rebuilding a client while CONTAINER_HOST still points at a
    dead socket reconnects to the same dead path, so the retry fails for
    exactly the reason the first attempt did. See reset_podman_transport.
    """
    _clients.clear()
    _probe_failures.clear()
    reset_podman_transport()


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


#: Docker's exec stream frames an 8-byte header before each chunk:
#: [stream_type, 0, 0, 0, size:uint32be], stream 1 = stdout, 2 = stderr.
_FRAME_HEADER = 8


def _looks_framed(raw: bytes) -> bool:
    """Whether `raw` starts with something shaped like a frame header."""
    return (
        len(raw) >= _FRAME_HEADER
        and raw[0] in (0, 1, 2)
        and raw[1:4] == b"\x00\x00\x00"
    )


def demux_frames(raw: bytes) -> tuple[str, str]:
    """Split multiplexed exec output into (stdout, stderr).

    The two clients disagree about who does this, and the disagreement is
    silent. docker-py demultiplexes for you and hands back plain bytes.
    podman-py hands back the RAW framed stream, so code that decodes it
    directly gets a string full of header bytes that reads as output and is
    not. Measured on the same command:

        docker-py   b'MARKER-42\n'
        podman-py   b'\x01\x00\x00\x00\x00\x00\x00\tMARKER-42...'

    This cost a real bug: a check for surviving processes parsed podman's
    frame bytes, found no digits in them, and reported "nothing running" —
    on a container it had never actually read. It answered correctly by
    accident, which is worse than answering wrongly.

    Unframed input is returned as stdout unchanged, and anything that stops
    parsing cleanly falls back the same way, so this is safe on either
    client's result.
    """
    if not raw or not _looks_framed(raw):
        return raw.decode("utf-8", "replace"), ""
    out: list[str] = []
    err: list[str] = []
    at = 0
    while at + _FRAME_HEADER <= len(raw):
        kind = raw[at]
        size = int.from_bytes(raw[at + 4 : at + _FRAME_HEADER], "big")
        at += _FRAME_HEADER
        chunk = raw[at : at + size]
        if len(chunk) < size:
            # Truncated: trust nothing after this point rather than
            # inventing a boundary.
            break
        at += size
        (err if kind == 2 else out).append(chunk.decode("utf-8", "replace"))
    if at == 0:
        return raw.decode("utf-8", "replace"), ""
    return "".join(out), "".join(err)


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


def list_managed(
    backend: str, label: str, cli_timeout: float = PODMAN_CLI_TIMEOUT
) -> list[Any]:
    """Every container carrying `label`. Raises if the engine is down —
    an empty list must mean 'none', never 'could not ask'."""
    def op():
        engine = client(backend, cli_timeout=cli_timeout)
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


@dataclass
class Resolution:
    """Which engine was chosen, how it was reached, and what was not.

    The rejected list is the point. "No container engine is reachable" is
    a true statement that helps nobody; naming what was tried and what each
    one said is the difference between a message and a diagnosis.
    """

    backend: str
    endpoint: str
    version: str
    product: str
    rejected: list[tuple[str, str]]

    def banner(self) -> str:
        lines = []
        for name, why in self.rejected:
            lines.append(f"  {name:8} not reachable — {why}")
        lines.append(
            f"  {self.product:8} ready — {self.product} {self.version} "
            f"via {self.endpoint}"
        )
        lines.append(f"using {self.product}")
        return "\n".join(lines)


def resolve(preferred: str = "auto") -> Resolution:
    """Pick an engine and report how, without raising for the ones that failed.

    Goes through the REST client rather than an SDK, so the answer names
    the endpoint actually dialled. `auto` prefers docker and falls back —
    a missing engine is never an error while the other one works, which is
    the behaviour `hyperbox build` was lacking when it failed outright on a
    machine with a perfectly good Podman.
    """
    from hyperbox_mcp.rest import api
    from hyperbox_mcp.rest.client import EngineClient

    if preferred != "auto" and preferred not in BACKENDS:
        raise UnsupportedBackendError(
            f"Unsupported backend '{preferred}'. Supported: auto, "
            f"{', '.join(BACKENDS)}"
        )
    candidates = BACKENDS if preferred == "auto" else (preferred,)
    rejected: list[tuple[str, str]] = []
    for backend in candidates:
        try:
            target = endpoint_for(backend)
            client = EngineClient(target)
            product = api.identify(client)
            version = str(client.version.get("Version", "?"))
        except Exception as exc:  # noqa: BLE001 - collected, then reported
            message = getattr(exc, "message", None) or str(exc)
            rejected.append((backend, message.split("\n")[0][:110]))
            continue
        if preferred != "auto" and product != preferred:
            # Asking for Docker and being handed Podman is a wrong answer,
            # not a successful resolution.
            rejected.append(
                (backend, f"that endpoint is served by {product}, not {preferred}")
            )
            continue
        return Resolution(backend, target, version, product, rejected)

    raise NoEngineError(
        "No container engine is reachable, so there is nowhere to run code.\n"
        + "\n".join(f"  {name}: {why}" for name, why in rejected),
        fix="Start one:\n"
            "  Docker  — open Docker Desktop, or `sudo systemctl start docker`\n"
            "  Podman  — `podman machine start` (first time: `podman machine init`)",
        context={"tried": [name for name, _ in rejected]},
    )


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
