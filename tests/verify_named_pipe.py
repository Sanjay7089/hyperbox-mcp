"""Windows named-pipe transport, proven without a container engine.

    python tests/verify_named_pipe.py

The v0.3 REST driver reaches Docker and Podman over a unix socket on POSIX
and a named pipe on Windows. The pipe half is hand-written ctypes — neither
httpx nor httpcore has an npipe transport — which makes it the riskiest
code in the driver and the least covered: GitHub's Windows runners ship no
container engine, so nothing on CI can exercise it against a real one.

This closes that gap. A stub pipe server speaks just enough of the Docker
REST API to exercise every path the transport has: a normal JSON response,
a chunked one, and the hijacked, 8-byte-framed exec stream that is the part
that actually breaks. Measured against Podman 5.5.1 on Windows, that stream
ends with ERROR_INVALID_HANDLE rather than the ERROR_BROKEN_PIPE a socket
would give, and treating that as a failure discarded a completed exec.

Skips cleanly off Windows: there is nothing here to test on a platform
without named pipes.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
import time

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

WINDOWS = sys.platform == "win32"
PIPE = r"\\.\pipe\hyperbox-verify-npipe"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)
    if detail:
        print(f"        {detail}")


def frame(stream: int, text: str) -> bytes:
    """One Docker exec frame: [type,0,0,0,size:uint32be] + payload."""
    body = text.encode()
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(body)) + body


#: What the stub answers, keyed by the first line of the request.
BODY_VERSION = json.dumps(
    {"Version": "0.0-stub", "ApiVersion": "1.44", "MinAPIVersion": "1.24",
     "Components": [{"Name": "Podman Engine"}]}
).encode()

#: Big enough, and sent late enough, that it CANNOT be pre-buffered.
#:
#: The first version of this was 50 bytes sent immediately, so header
#: parsing pulled the whole body into the BufferedReader and the test
#: passed while the transport was closing the pipe out from under the
#: reader. It passed on a shim that returned nothing at all against real
#: Podman. A payload larger than the buffer, written after the headers,
#: forces a read that actually touches the handle.
BULK = "x" * 60000
HIJACK = (
    frame(1, "hyperbox_pipe_ok")
    + frame(2, "to-stderr")
    + frame(1, BULK)
    + frame(1, "\n")
)


def serve_once(ready: threading.Event, stop: threading.Event) -> None:
    """A one-shot named-pipe HTTP server, in ctypes like the client."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PIPE_ACCESS_DUPLEX = 0x3
    PIPE_TYPE_BYTE = 0x0
    PIPE_WAIT = 0x0
    k32.CreateNamedPipeW.restype = wintypes.HANDLE
    k32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    ready.set()
    while not stop.is_set():
        handle = k32.CreateNamedPipeW(
            PIPE, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE | PIPE_WAIT,
            255, 65536, 65536, 0, None,
        )
        if handle == ctypes.c_void_p(-1).value:
            return
        if not k32.ConnectNamedPipe(handle, None):
            err = ctypes.get_last_error()
            if err != 535:  # ERROR_PIPE_CONNECTED
                k32.CloseHandle(handle)
                continue
        try:
            buf = (ctypes.c_char * 65536)()
            read = wintypes.DWORD(0)
            k32.ReadFile(handle, buf, 65536, ctypes.byref(read), None)
            request = bytes(buf[: read.value])
            head = request.split(b"\r\n", 1)[0]

            if b"/exec/" in head and b"/start" in head:
                # Hijacked: headers, then raw frames, then hang up. No
                # Content-Length -- so http.client marks the response
                # will_close and closes the socket immediately, while the
                # body is still being read. That is the case that returned
                # zero bytes against real Podman.
                #
                # Headers and body are written SEPARATELY with a pause
                # between, so the body cannot arrive in time to be buffered
                # during header parsing. Without the pause this test passes
                # against a transport that does not work.
                headers = (b"HTTP/1.1 200 OK\r\n"
                           b"Content-Type: application/vnd.docker.raw-stream\r\n"
                           b"\r\n")
                written = wintypes.DWORD(0)
                k32.WriteFile(handle, headers, len(headers),
                              ctypes.byref(written), None)
                k32.FlushFileBuffers(handle)
                time.sleep(0.25)
                resp = HIJACK
            elif b"/chunked" in head:
                resp = (b"HTTP/1.1 200 OK\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n"
                        b"5\r\nhello\r\n6\r\n-world\r\n0\r\n\r\n")
            else:
                resp = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(BODY_VERSION)).encode()
                        + b"\r\n\r\n" + BODY_VERSION)
            written = wintypes.DWORD(0)
            k32.WriteFile(handle, resp, len(resp), ctypes.byref(written), None)
            k32.FlushFileBuffers(handle)
        finally:
            k32.DisconnectNamedPipe(handle)
            k32.CloseHandle(handle)


def main() -> int:
    print(f"named-pipe transport checks on {sys.platform}\n")
    if not WINDOWS:
        print("SKIP  every check: named pipes are Windows-only.")
        print("\nNothing to prove on this platform.")
        return 0

    # The package module, not the spike: this test exists to cover the
    # code that ships.
    from hyperbox_mcp.engine import demux_frames as demux
    from hyperbox_mcp.rest.transport import NamedPipeHTTPConnection

    # Several listeners, not one. A single-instance server recreates its
    # pipe between requests, and a client connecting in that window gets
    # ERROR_FILE_NOT_FOUND -- a race in the stub that looks exactly like a
    # transport bug. CreateNamedPipe allows many instances of one name, so
    # there is always one waiting.
    ready, stop = threading.Event(), threading.Event()
    for _ in range(4):
        threading.Thread(
            target=serve_once, args=(ready, stop), daemon=True
        ).start()
    ready.wait(5)
    time.sleep(0.5)

    def get(path: str):
        conn = NamedPipeHTTPConnection(PIPE, timeout=15)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    try:
        status, body = get("/version")
        info = json.loads(body) if status == 200 else {}
        check(
            "a JSON response with Content-Length round-trips",
            status == 200 and info.get("ApiVersion") == "1.44",
            f"status={status} ApiVersion={info.get('ApiVersion')!r}",
        )

        status, body = get("/chunked")
        check(
            "a chunked response is de-chunked by http.client",
            status == 200 and body == b"hello-world",
            f"status={status} body={body!r}",
        )

        # The one that matters: no Content-Length, ends by hanging up.
        conn = NamedPipeHTTPConnection(PIPE, timeout=15)
        try:
            conn.request("POST", "/exec/stub/start", body=b"{}")
            r = conn.getresponse()
            raw = r.read()
        finally:
            conn.close()
        out, err = demux(raw)
        check(
            "a hijacked stream is read to the end, not lost at the hangup",
            "hyperbox_pipe_ok" in out and "to-stderr" in err
            and out.count("x") == len(BULK),
            f"{len(raw)} bytes -> {len(out)} stdout, {len(err)} stderr "
            f"(bulk {out.count('x')}/{len(BULK)})",
        )
        check(
            "its frames demultiplex into separate streams",
            out.strip() == "hyperbox_pipe_ok" and err.strip() == "to-stderr",
            "stdout and stderr did not bleed into each other",
        )

        try:
            NamedPipeHTTPConnection(r"\\.\pipe\hyperbox-definitely-absent").connect()
            missing = "connected to a pipe that does not exist"
        except ConnectionRefusedError as exc:
            missing = f"{type(exc).__name__}: {str(exc)[:60]}"
        check(
            "an absent pipe raises ConnectionRefusedError, not a WinError blob",
            "ConnectionRefusedError" in missing,
            missing,
        )
    finally:
        stop.set()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    if passed != len(results):
        print("\nThe named-pipe transport is NOT sound here.")
        return 1
    print("\nThe named-pipe transport handles every shape the engine uses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
