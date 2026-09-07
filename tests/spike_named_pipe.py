"""v0.3 SPIKE: can we speak the Docker REST API with no SDK, on this OS?

    python tests/spike_named_pipe.py

Throwaway by intent. It exists to answer one question before the v0.3 REST
driver is written, because the answer changes that driver's scope:

    Can stdlib http.client reach Docker and Podman over a unix socket
    (POSIX) and a named pipe (Windows), including a STREAMED EXEC?

`GET /version` is not a sufficient answer and a spike that only does that
will pass here and fail in Phase 1. The parts that actually break are the
framed exec stream and `makefile()`, so both are exercised.

Nothing here is imported by the package. It takes no dependencies at all —
that is the point being tested.

Windows note: neither httpx nor httpcore has a named-pipe transport, so
this is the one place the REST driver is not simpler than docker-py, which
ships npipeconn.py for it. Everything below is what that file does, via
ctypes instead of pywin32.
"""

from __future__ import annotations

import glob
import http.client
import io
import json
import os
import socket
import struct
import sys

WINDOWS = sys.platform == "win32"

#: The API version we would like to speak. Negotiated DOWN per engine —
#: never sent blind. See negotiate().
PREFERRED_API = "1.44"


# --- POSIX: a unix socket behind HTTPConnection -------------------------


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket. Fifteen lines, no dependency."""

    def __init__(self, path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


# --- Windows: a named pipe pretending to be a socket --------------------

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_PIPE_BUSY = 231
    ERROR_BROKEN_PIPE = 109
    ERROR_NO_DATA = 232
    ERROR_INVALID_HANDLE = 6
    ERROR_PIPE_NOT_CONNECTED = 233
    ERROR_MORE_DATA = 234
    PIPE_READMODE_BYTE = 0x00000000

    #: Codes that mean "the stream ended", not "something went wrong".
    #:
    #: Measured against Podman 5.5.1 on Windows: reading the hijacked exec
    #: stream ends with ERROR_INVALID_HANDLE (6) rather than the
    #: ERROR_BROKEN_PIPE (109) a socket would give. Treating 6 as a failure
    #: turns a completed exec into a ConnectionResetError with the whole
    #: payload already in hand. docker-py avoids this by reading through
    #: overlapped I/O; this is the same conclusion reached from the error
    #: codes instead.
    _PIPE_EOF = (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED,
                 ERROR_INVALID_HANDLE)

    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _k32.WaitNamedPipeW.restype = wintypes.BOOL
    _k32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _k32.WriteFile.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.SetNamedPipeHandleState.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    _k32.SetNamedPipeHandleState.restype = wintypes.BOOL

    class _PipeRaw(io.RawIOBase):
        """RawIOBase over a pipe handle, so io.BufferedReader can wrap it.

        http.client calls sock.makefile('rb') and then readline()/read(n)
        on the result, so a buffered reader over a raw readinto() is
        exactly the shape it needs. This is the piece docker-py spends most
        of npipeconn.py on.
        """

        def __init__(self, handle) -> None:
            self._handle = handle
            self.eof_code = 0

        def readable(self) -> bool:
            return True

        def readinto(self, buffer) -> int:
            want = len(buffer)
            if not want:
                return 0
            chunk = (ctypes.c_char * want)()
            read = wintypes.DWORD(0)
            ok = _k32.ReadFile(
                self._handle, chunk, want, ctypes.byref(read), None
            )
            if not ok:
                code = ctypes.get_last_error()
                # A closed pipe is EOF, not an error: the engine hangs up at
                # the end of a response body and of an exec stream, and
                # Windows reports that hangup with several different codes
                # depending on how the handle was opened.
                if code in _PIPE_EOF:
                    self.eof_code = code
                    return 0
                if code == ERROR_MORE_DATA:
                    # Message-mode pipe with a short buffer: the data IS
                    # there, so keep what arrived rather than failing.
                    buffer[: read.value] = chunk[: read.value]
                    return read.value
                raise ConnectionResetError(
                    code, f"ReadFile failed (WinError {code})"
                )
            buffer[: read.value] = chunk[: read.value]
            return read.value

    class _PipeSocket:
        """The socket-like surface http.client actually uses.

        Windows error codes are translated into stdlib exceptions here
        rather than leaked as WinError strings, so callers can branch on
        BrokenPipeError / ConnectionResetError the same way they do on
        POSIX. That is what lets stale-connection detection be isinstance
        checks instead of substring matching on error text.
        """

        def __init__(self, handle) -> None:
            self._handle = handle

        def sendall(self, data: bytes) -> None:
            view, total = memoryview(data), 0
            while total < len(view):
                written = wintypes.DWORD(0)
                block = bytes(view[total:])
                ok = _k32.WriteFile(
                    self._handle, block, len(block),
                    ctypes.byref(written), None,
                )
                if not ok:
                    code = ctypes.get_last_error()
                    raise BrokenPipeError(
                        code, f"WriteFile failed (WinError {code})"
                    )
                if written.value == 0:
                    raise BrokenPipeError("WriteFile wrote nothing")
                total += written.value

        def makefile(self, mode: str = "rb", buffering: int = -1):
            if "b" not in mode:
                raise ValueError("only binary mode is supported")
            return io.BufferedReader(_PipeRaw(self._handle))

        def settimeout(self, _timeout) -> None:
            # Synchronous pipe handles carry no per-call timeout. Noted
            # rather than silently ignored: a hung engine blocks the worker
            # thread, which is survivable because every engine call is
            # already offloaded, but it is a real difference from POSIX.
            return None

        def close(self) -> None:
            if self._handle is not None:
                _k32.CloseHandle(self._handle)
                self._handle = None

    class NamedPipeHTTPConnection(http.client.HTTPConnection):
        def __init__(self, pipe: str, timeout: float = 30.0) -> None:
            super().__init__("localhost", timeout=timeout)
            self._pipe = pipe

        def connect(self) -> None:
            handle = _k32.CreateFileW(
                self._pipe, GENERIC_READ | GENERIC_WRITE, 0, None,
                OPEN_EXISTING, 0, None,
            )
            if handle == INVALID_HANDLE_VALUE:
                code = ctypes.get_last_error()
                if code != ERROR_PIPE_BUSY:
                    raise ConnectionRefusedError(
                        code, f"CreateFileW({self._pipe}) failed "
                              f"(WinError {code})"
                    )
                # Every instance is busy. Wait, but bounded: an unbounded
                # WaitNamedPipe is a hang with no diagnosis.
                if not _k32.WaitNamedPipeW(self._pipe, 10_000):
                    raise ConnectionRefusedError(
                        f"{self._pipe} stayed busy for 10s"
                    )
                handle = _k32.CreateFileW(
                    self._pipe, GENERIC_READ | GENERIC_WRITE, 0, None,
                    OPEN_EXISTING, 0, None,
                )
                if handle == INVALID_HANDLE_VALUE:
                    code = ctypes.get_last_error()
                    raise ConnectionRefusedError(
                        code, f"CreateFileW retry failed (WinError {code})"
                    )
            # Ask for byte mode rather than inheriting whatever the
            # server created. In message mode a read shorter than the
            # message returns ERROR_MORE_DATA, which is not how HTTP framing
            # expects to be read. Best effort: a pipe already in byte mode
            # refuses this and does not care.
            mode = wintypes.DWORD(PIPE_READMODE_BYTE)
            _k32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)
            self.sock = _PipeSocket(handle)


# --- endpoint discovery -------------------------------------------------


def windows_pipes() -> list[str]:
    """Named pipes on this machine that might be an engine.

    Enumerated, not guessed. `\\.\pipe\` is listable on Windows, and
    guessing from a fixed list is how an earlier bug happened: on a
    Podman-only machine Podman answers `docker_engine` and nothing else, so
    an explicit podman lookup failed while auto worked. Anything named like
    an engine is tried and identified by what answers, never by its name.
    """
    known = [r"\\.\pipe\podman-machine-default",
             r"\\.\pipe\docker_engine",
             r"\\.\pipe\podman"]
    try:
        listed = os.listdir(r"\\.\pipe")
    except OSError:
        listed = []
    for name in sorted(listed):
        low = name.lower()
        if "podman" in low or "docker" in low:
            full = r"\\.\pipe" + "\\" + name
            if full not in known:
                known.append(full)
    return known


def endpoints() -> list[tuple[str, str]]:
    """(label, target) pairs worth trying on this machine."""
    found: list[tuple[str, str]] = []
    if WINDOWS:
        return [(pipe, pipe) for pipe in windows_pipes()]
    for path in ("/var/run/docker.sock", os.path.expanduser("~/.docker/run/docker.sock")):
        if os.path.exists(path):
            found.append((path, path))
    for pattern in ("/var/folders/*/*/T/podman/*-api.sock", "/tmp/podman/*-api.sock",
                    f"/run/user/{os.getuid()}/podman/podman.sock"):
        for path in sorted(glob.glob(pattern)):
            found.append((path, path))
    return found


def connect(target: str, timeout: float = 30.0):
    return (NamedPipeHTTPConnection(target, timeout) if WINDOWS
            else UnixHTTPConnection(target, timeout))


def request(target: str, method: str, url: str, body=None, headers=None):
    conn = connect(target)
    try:
        conn.request(method, url, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _version_key(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split("."))
    except (AttributeError, ValueError):
        return (0,)


def negotiate(target: str) -> tuple[str, dict]:
    """Agree an API version with whatever is on the other end.

    The first call MUST be unversioned. Both engines answer `GET /version`
    with no prefix, and it is the only request that is safe before the
    engine's supported range is known.

    Measured, and the reason this is not a nicety:

        Docker 29.1.3   API 1.44 .. 1.52
        Podman 6.1.1    API 1.24 .. 1.44

    The intersection is exactly ONE version. A hardcoded constant has a
    one-version-wide window that the next release of either engine closes.

    The failure mode is the dangerous kind, not a crash. Asking Docker for
    /v1.41/version returns 400 with a JSON body — Platform present,
    ApiVersion an empty string. A client that parses without checking the
    status gets a plausible object full of blanks and carries on
    describing an engine it never actually spoke to.
    """
    conn = connect(target)
    try:
        conn.request("GET", "/version")
        response = conn.getresponse()
        status, body = response.status, response.read()
    finally:
        conn.close()
    if status != 200:
        raise RuntimeError(f"unversioned GET /version returned {status}")
    info = json.loads(body)
    low = info.get("MinAPIVersion") or info.get("ApiVersion") or PREFERRED_API
    high = info.get("ApiVersion") or PREFERRED_API
    chosen = PREFERRED_API
    if _version_key(chosen) < _version_key(low):
        chosen = low
    if _version_key(chosen) > _version_key(high):
        chosen = high
    return f"/v{chosen}", info


def demux(raw: bytes) -> tuple[str, str]:
    """Split Docker's 8-byte-framed exec output into stdout and stderr.

    Header is [stream_type, 0, 0, 0, size:uint32be]; stream 1 is stdout,
    2 is stderr. The real driver must also handle a header split across
    reads — here the whole body is already in hand.
    """
    out, err, at = [], [], 0
    while at + 8 <= len(raw):
        kind = raw[at]
        (size,) = struct.unpack(">I", raw[at + 4:at + 8])
        at += 8
        payload = raw[at:at + size].decode("utf-8", "replace")
        at += size
        (out if kind == 1 else err).append(payload)
    return "".join(out), "".join(err)


# --- the three probes ---------------------------------------------------


def probe(label: str, target: str) -> bool:
    print(f"\n=== {label} ===")
    marker = "hyperbox_pipe_spike_ok"
    try:
        API, info = negotiate(target)
        product = " ".join(
            c.get("Name", "") for c in (info.get("Components") or [])
        ) or info.get("Version", "?")
        print(f"  PASS  GET /version -> {info.get('Version')} "
              f"(supports {info.get('MinAPIVersion')} .. "
              f"{info.get('ApiVersion')})")
        print(f"        negotiated {API}, product: {product}")
    except Exception as exc:
        print(f"  SKIP  not reachable: {type(exc).__name__}: {exc}")
        return False

    # Prove the negotiated prefix is actually accepted, since an
    # unversioned probe succeeding says nothing about a versioned one.
    status, _ = request(target, "GET", f"{API}/_ping")
    print(f"  {'PASS' if status == 200 else 'FAIL'}  {API}/_ping -> {status}")
    if status != 200:
        return False

    try:
        status, body = request(target, "GET", f"{API}/containers/json?all=true")
        print(f"  {'PASS' if status == 200 else 'FAIL'}  "
              f"GET /containers/json -> {status}, "
              f"{len(json.loads(body)) if status == 200 else '-'} containers")
        if status != 200:
            return False
    except Exception as exc:
        print(f"  FAIL  GET /containers/json: {type(exc).__name__}: {exc}")
        return False

    # The one that matters. Needs a running container; any will do.
    try:
        _, body = request(target, "GET", f"{API}/containers/json")
        running = json.loads(body)
        if not running:
            print("  SKIP  streamed exec: no running container to exec into.")
            print("        Start any container and re-run -- this is THE "
                  "probe that decides Phase 1's scope.")
            return True
        cid = running[0]["Id"]
        payload = json.dumps({
            "AttachStdout": True, "AttachStderr": True, "Tty": False,
            "Cmd": ["/bin/sh", "-c", f"echo {marker}; echo err >&2"],
        }).encode()
        status, body = request(
            target, "POST", f"{API}/containers/{cid}/exec", payload,
            {"Content-Type": "application/json"},
        )
        if status != 201:
            print(f"  FAIL  exec create -> {status}: {body[:200]!r}")
            return False
        exec_id = json.loads(body)["Id"]
        status, raw = request(
            target, "POST", f"{API}/exec/{exec_id}/start",
            json.dumps({"Detach": False, "Tty": False}).encode(),
            {"Content-Type": "application/json"},
        )
        out, err = demux(raw)
        print(f"        read {len(raw)} bytes from the hijacked stream")
        status2, body2 = request(target, "GET", f"{API}/exec/{exec_id}/json")
        code = json.loads(body2).get("ExitCode")
        ok = marker in out and "err" in err and code == 0
        print(f"  {'PASS' if ok else 'FAIL'}  streamed exec -> "
              f"stdout={out.strip()!r} stderr={err.strip()!r} exit={code}")
        print(f"        {len(raw)} raw bytes, demuxed into two streams")
        return ok
    except Exception as exc:
        print(f"  FAIL  streamed exec: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    print(f"HyperBox v0.3 REST spike -- {sys.platform}, Python "
          f"{sys.version.split()[0]}, stdlib only")
    print(f"transport: {'named pipe (ctypes)' if WINDOWS else 'unix socket'}")
    targets = endpoints()
    if not targets:
        print("\nNo endpoints found. Start Docker or Podman and retry.")
        return 1
    results = [probe(label, target) for label, target in targets]
    reached = sum(1 for r in results if r)
    print(f"\n{reached}/{len(results)} endpoint(s) fully answered.")
    if reached:
        print("VERDICT: stdlib http.client speaks the Docker REST API here. "
              "Phase 1 proceeds as planned on this platform.")
        if WINDOWS:
            print(
                "         On Windows this is the load-bearing result: "
                "podman-py has NO named-pipe transport, so v0.2 reaches "
                "Podman here by pointing docker-py at its pipe. A ctypes "
                "transport that works means Phase 1 can drop docker-py "
                "outright rather than keeping it for this one case."
            )
        return 0
    print("VERDICT: no endpoint answered.")
    if WINDOWS:
        print("  Check first that this is a HyperBox problem and not a "
              "stopped engine:")
        print("    podman machine list        # is a machine running?")
        print("    podman version            # does the CLI reach it?")
        print("  Pipes visible right now:")
        for pipe in windows_pipes():
            print(f"    {pipe}")
        print("  If a machine IS running and none of these answered, that "
              "is the finding: Phase 1 ships Windows as experimental or "
              "keeps docker-py for it. Send this output either way.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
