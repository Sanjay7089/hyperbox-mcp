"""A cross-process file lock that works on POSIX and Windows.

The registry needs mutual exclusion per sandbox, held across processes,
with one property that is not negotiable: **the OS must release the lock
when the holder dies**. A server killed mid-operation must not wedge a
sandbox forever, and no cleanup handler can be relied on for that — the
process may be gone without warning.

Both implementations below get that from the OS, because both attach the
lock to an open file handle rather than to a file's existence:

- POSIX: `fcntl.flock`, released when the fd closes or the process dies.
- Windows: `msvcrt.locking`, likewise released when the handle closes.

A lock file whose mere existence means "locked" is NOT equivalent, and is
why this module does not simply create and delete a file: a crash would
leave the sandbox permanently unusable.

`msvcrt.locking(LK_LOCK)` retries for only ten seconds and then raises,
which is far too short for container work, so Windows polls the
non-blocking variant instead and controls its own deadline.
"""

from __future__ import annotations

import sys
import time
from typing import IO

#: How long to wait for another process to release a sandbox's lock
#: before giving up. Longer than the slowest thing done under one:
#: creating a sandbox, which includes a possible image pull.
DEFAULT_LOCK_TIMEOUT = 300.0

WINDOWS = sys.platform == "win32"

if WINDOWS:  # pragma: no cover - platform-specific
    import msvcrt

    def _acquire(handle: IO, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        delay = 0.01
        while True:
            try:
                handle.seek(0)
                # One byte is enough: every holder locks the same byte.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for another HyperBox process to "
                        "release this sandbox's lock."
                    ) from None
                time.sleep(delay)
                delay = min(delay * 2, 0.25)

    def _release(handle: IO) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            # Already gone, or the handle died with the process. Either
            # way the lock is not held any more.
            pass

else:
    import fcntl

    def _acquire(handle: IO, timeout: float) -> None:  # noqa: ARG001
        # flock blocks until the lock is free and is interrupted by
        # signals, so it needs no deadline of its own.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _release(handle: IO) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class FileLock:
    """Exclusive, cross-process lock on one path.

    Used as a context manager. The file is created if absent and never
    deleted: removing it would let two processes lock two different
    inodes under the same name and both believe they hold it.
    """

    def __init__(self, path, timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: IO | None = None

    def __enter__(self) -> "FileLock":
        # "a+" so the file is created if missing and never truncated —
        # truncation would race with another process's handle.
        handle = open(self.path, "a+")
        try:
            _acquire(handle, self.timeout)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, *exc) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _release(handle)
        finally:
            handle.close()
