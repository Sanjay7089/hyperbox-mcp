"""One HTTP client for both container engines.

Docker and Podman answer the same REST API, so this speaks it once. The
three shapes an engine replies in each need different handling and each has
a way of failing quietly:

    a JSON body          Content-Length, ordinary
    a progress stream    newline-delimited JSON, for pulls and builds
    a hijacked stream    8-byte-framed exec output, no length at all

The API version is NEGOTIATED, never assumed. Measured across three
engines:

    Docker 29.1.3    1.44 .. 1.52
    Podman 6.1.1     1.24 .. 1.44
    Podman 5.5.1     1.24 .. 1.41

The overlap is one version wide and moves with every release, so a
constant is a bug waiting for someone else's upgrade. Worse, asking Docker
for a version below its floor does not look like an error: it returns
**400 with a well-formed JSON body** whose `ApiVersion` is an empty string.
A client that parses without checking the status gets a plausible object
full of blanks and goes on describing an engine it never spoke to.

So the first request is always unversioned — `GET /version`, which every
engine answers — and the prefix is confirmed with `/_ping` before anything
depends on it.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterator

from hyperbox_mcp import errors
from hyperbox_mcp.rest.transport import connection_for

#: What we would like to speak. Clamped into whatever the engine supports.
PREFERRED_API = "1.44"

#: Docker's exec framing: [stream, 0, 0, 0, size:uint32be] then payload.
FRAME_HEADER = 8
STDOUT, STDERR = 1, 2


def _version_key(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except (AttributeError, ValueError):
        return (0,)


class FrameReader:
    """Incremental demultiplexer for a hijacked exec stream.

    Incremental because the read boundaries have nothing to do with the
    frame boundaries: a single recv can return three bytes of a header, or
    a header plus half its payload, or two whole frames and a fragment.
    Parsing each chunk independently silently corrupts output, and the
    corruption looks like the program's own.

    A zero-length frame is a frame, not the end: the engine emits them, and
    treating one as EOF truncates everything after it.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.stdout: list[str] = []
        self.stderr: list[str] = []

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while len(self._buffer) >= FRAME_HEADER:
            kind = self._buffer[0]
            size = int.from_bytes(self._buffer[4:FRAME_HEADER], "big")
            if len(self._buffer) < FRAME_HEADER + size:
                return  # the payload has not all arrived yet
            payload = bytes(self._buffer[FRAME_HEADER : FRAME_HEADER + size])
            del self._buffer[: FRAME_HEADER + size]
            text = payload.decode("utf-8", "replace")
            (self.stderr if kind == STDERR else self.stdout).append(text)

    def result(self) -> tuple[str, str]:
        """What arrived. A trailing partial frame is discarded rather than
        guessed at — inventing a boundary would fabricate output."""
        return "".join(self.stdout), "".join(self.stderr)


class EngineClient:
    """A live connection to one engine, addressed by socket path or pipe."""

    def __init__(self, target: str, timeout: float = 60.0) -> None:
        self.target = target
        self.timeout = timeout
        self._api: str | None = None
        self._version: dict[str, Any] = {}

    # --- negotiation --------------------------------------------------

    def _raw(self, method: str, path: str, body=None, headers=None):
        conn = connection_for(self.target, self.timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    @property
    def api(self) -> str:
        """The negotiated version prefix, e.g. "/v1.44"."""
        if self._api is None:
            self._negotiate()
        return self._api  # type: ignore[return-value]

    @property
    def version(self) -> dict[str, Any]:
        if self._api is None:
            self._negotiate()
        return self._version

    def _negotiate(self) -> None:
        try:
            status, body = self._raw("GET", "/version")
        except OSError as exc:
            raise errors.EngineUnavailableError(
                f"Could not reach the engine at {self.target} "
                f"({type(exc).__name__}: {exc}).",
                fix="Start Docker or Podman, then try again.",
                context={"endpoint": self.target},
            ) from exc
        if status != 200:
            raise errors.EngineUnavailableError(
                f"The engine at {self.target} answered {status} to an "
                "unversioned version request.",
                context={"endpoint": self.target, "status": status},
            )
        info = json.loads(body)
        low = info.get("MinAPIVersion") or info.get("ApiVersion") or PREFERRED_API
        high = info.get("ApiVersion") or PREFERRED_API
        chosen = PREFERRED_API
        if _version_key(chosen) < _version_key(low):
            chosen = low
        if _version_key(chosen) > _version_key(high):
            chosen = high
        prefix = f"/v{chosen}"
        # Confirm rather than assume: an unversioned probe succeeding says
        # nothing about whether a versioned path is accepted.
        status, _ = self._raw("GET", f"{prefix}/_ping")
        if status != 200:
            raise errors.EngineUnavailableError(
                f"The engine at {self.target} supports {low}..{high} but "
                f"refused {prefix} ({status}).",
                context={"endpoint": self.target},
            )
        self._api, self._version = prefix, info

    # --- the three response shapes ------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        expect: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """A JSON call. Returns the parsed body, or None for 204."""
        payload, headers = None, {}
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._raw(method, f"{self.api}{path}", payload, headers)
        if status == 404:
            raise errors.ContainerGoneError(
                _message(raw) or f"{path} does not exist on this engine.",
                context={"endpoint": self.target, "path": path},
            )
        if status not in expect:
            raise errors.EngineUnavailableError(
                f"{method} {path} returned {status}: {_message(raw)}",
                context={"endpoint": self.target, "status": status},
            )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def stream_json(
        self, method: str, path: str, body: bytes | None = None, headers=None
    ) -> Iterator[dict]:
        """Newline-delimited JSON progress events, as pulls and builds emit.

        Yielded as they arrive rather than collected, because the point of
        reading them is to show progress while the work is still happening.
        """
        conn = connection_for(self.target, self.timeout)
        try:
            conn.request(method, f"{self.api}{path}", body=body,
                         headers=headers or {})
            response = conn.getresponse()
            if response.status not in (200, 201):
                raise errors.EngineUnavailableError(
                    f"{method} {path} returned {response.status}: "
                    f"{_message(response.read())}",
                    context={"endpoint": self.target},
                )
            pending = b""
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            if pending.strip():
                try:
                    yield json.loads(pending)
                except json.JSONDecodeError:
                    pass
        finally:
            conn.close()

    def stream_frames(
        self,
        path: str,
        body: dict,
        on_chunk: Callable[[int, str], None] | None = None,
    ) -> tuple[str, str]:
        """POST and demultiplex a hijacked exec stream into (stdout, stderr).

        `Tty` must be false and is asserted, not documented: a TTY stream
        carries no framing at all, so the same parser would read the
        program's own output as frame headers and return nonsense that
        looks like output.
        """
        if body.get("Tty"):
            raise ValueError(
                "Tty must be false: a TTY exec stream is unframed, and "
                "reading it as frames corrupts the output silently."
            )
        payload = json.dumps(body).encode()
        conn = connection_for(self.target, self.timeout)
        try:
            conn.request(
                "POST", f"{self.api}{path}", body=payload,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            if response.status not in (200, 101):
                raise errors.EngineUnavailableError(
                    f"POST {path} returned {response.status}: "
                    f"{_message(response.read())}",
                    context={"endpoint": self.target},
                )
            reader = FrameReader()
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                before = (len(reader.stdout), len(reader.stderr))
                reader.feed(chunk)
                if on_chunk:
                    for text in reader.stdout[before[0]:]:
                        on_chunk(STDOUT, text)
                    for text in reader.stderr[before[1]:]:
                        on_chunk(STDERR, text)
            return reader.result()
        finally:
            conn.close()


def _message(raw: bytes) -> str:
    """The engine's own error text, which it returns as {"message": ...}."""
    try:
        return json.loads(raw).get("message", "") or raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return raw.decode("utf-8", "replace")[:200] if raw else ""
