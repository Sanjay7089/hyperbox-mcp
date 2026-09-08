"""Reaching a container engine's HTTP API on this platform.

Two transports behind one `http.client.HTTPConnection`, because that is the
only difference between the platforms: POSIX has a unix socket, Windows has
a named pipe, and the HTTP spoken over each is identical.

The pipe half is hand-written ctypes. That is not a preference — neither
httpx nor httpcore has a named-pipe transport, so this code has to exist
whichever HTTP library is used, and writing it against the standard library
means no dependency at all. docker-py solves the same problem in
npipeconn.py with pywin32.

Measured, and the reason the error handling below is specific: reading a
hijacked exec stream from Podman 5.5.1 on Windows ends with
ERROR_INVALID_HANDLE (6), not the ERROR_BROKEN_PIPE (109) a socket gives at
the same point. Treating 6 as a failure discards a completed exec whose
payload is already in hand. Windows error codes are translated into stdlib
exceptions here so callers can branch on BrokenPipeError and
ConnectionResetError the same way on both platforms — which is what lets
stale-connection detection be isinstance checks rather than substring
matching on error text.

Proven by tests/verify_named_pipe.py against a stub pipe server, so the
platform where this code is bespoke is not the platform nothing tests it
on.
"""

from __future__ import annotations

import http.client
import io
import socket
import sys

WINDOWS = sys.platform == "win32"


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
    #: ERROR_INVALID_HANDLE is deliberately NOT here. It was, briefly, after
    #: a hijacked exec against Podman on Windows ended with it — and that
    #: read the symptom as the cause. The handle really had been closed:
    #: http.client closes the connection as soon as it sees a response with
    #: no length (`will_close`), and this shim was closing the pipe out from
    #: under the reader that was still holding it. Treating 6 as
    #: end-of-stream turned that into a silent zero-byte read, which is the
    #: exact failure this project exists to prevent.
    #:
    #: With the handle refcounted (see below) an invalid handle can only
    #: mean a real bug, so it stays an error and stays loud.
    _PIPE_EOF = (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED)

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

        def __init__(self, sock) -> None:
            # Holds the socket, not the handle: closing this reader has to
            # release a reference rather than close the pipe, because the
            # socket may still be in use — and vice versa.
            self._sock = sock
            self.eof_code = 0

        @property
        def _handle(self):
            return self._sock._handle

        def readable(self) -> bool:
            return True

        def close(self) -> None:
            if not self.closed:
                super().close()
                self._sock._release_io()

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
            # Reference counting, exactly as socket.socket does it.
            #
            # http.client hands the connection to the response and closes
            # the socket whenever a reply has no length — every hijacked
            # exec stream — while the file object returned by makefile() is
            # still reading from it. A real socket survives that because
            # makefile() takes a reference and the descriptor lives until
            # both are done. Without the same behaviour the pipe is closed
            # mid-body and every hijacked read returns nothing.
            self._io_refs = 0
            self._closed = False

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
            self._io_refs += 1
            return io.BufferedReader(_PipeRaw(self))

        def _release_io(self) -> None:
            """One file object is done with the pipe."""
            if self._io_refs > 0:
                self._io_refs -= 1
            if self._closed and self._io_refs <= 0:
                self._shut()

        def _shut(self) -> None:
            if self._handle is not None:
                _k32.CloseHandle(self._handle)
                self._handle = None

        def settimeout(self, _timeout) -> None:
            # Synchronous pipe handles carry no per-call timeout. Noted
            # rather than silently ignored: a hung engine blocks the worker
            # thread, which is survivable because every engine call is
            # already offloaded, but it is a real difference from POSIX.
            return None

        def close(self) -> None:
            """Close, unless a file object is still reading from it."""
            self._closed = True
            if self._io_refs <= 0:
                self._shut()

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


def connection_for(target: str, timeout: float = 30.0):
    """A connection to `target`, whatever kind of endpoint it names.

    Accepts a bare path or a scheme-qualified URL, because callers get
    these from three places that disagree about the form: CONTAINER_HOST
    and DOCKER_HOST carry `unix://` or `npipe://`, socket resolution
    returns a bare path, and Windows pipe discovery returns `\\.\pipe\…`.
    """
    if target.startswith("npipe://"):
        target = target[len("npipe://") :].replace("/", "\\")
    elif target.startswith("unix://"):
        target = target[len("unix://") :]
    if target.startswith("\\\\") or WINDOWS:
        if not WINDOWS:
            raise ValueError(f"named pipes are Windows-only: {target}")
        return NamedPipeHTTPConnection(target, timeout)
    return UnixHTTPConnection(target, timeout)
