"""Bounded concurrency against a shared container engine.

Several HyperBox servers commonly run at once — two editors, an agent in a
terminal, a second window of the same client — and they all drive one
engine over one socket. Heavy work done by all of them simultaneously is
where connections get dropped and calls start failing for reasons that have
nothing to do with the caller.

This is a ticket queue, not a lock. Slots live as files so the bound holds
ACROSS processes, which is the only place it matters: a semaphore inside
one process would not know the other three exist.

What is guarded, and what deliberately is not:

    create / pull / build   guarded. Minutes of work, megabytes of
                            transfer, and the operations that actually
                            saturate an engine.
    exec (run)              NOT guarded. A lightweight call on a
                            connection that already exists. Making every
                            run take a file lock would put latency on the
                            hot path to solve a problem it does not cause
                            — the contention is at connection setup.

The lock is held by an open file handle, so the OS releases it if the
holder dies. A killed server must not wedge a slot forever.
"""

from __future__ import annotations

import os
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hyperbox_mcp import errors
from hyperbox_mcp.filelock import FileLock

#: How many heavy operations may run at once, across every process.
#:
#: Four is enough to keep an engine busy and few enough that a laptop
#: pulling four images at once is still usable. Configurable because the
#: right number depends on the machine, not on this file.
DEFAULT_SLOTS = 4


def slot_count() -> int:
    raw = os.environ.get("HYPERBOX_ENGINE_SLOTS", "")
    if raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    return min(DEFAULT_SLOTS, max(1, os.cpu_count() or 1))


def _slot_dir() -> Path:
    from hyperbox_mcp.registry import state_dir

    directory = state_dir() / "engine.slots"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def active_holders() -> int:
    """How many slots are taken right now.

    Best effort, and only ever used to make a message concrete: "congested
    across 3 clients" is something a person can act on, where "try again"
    is not.
    """
    taken = 0
    for index in range(slot_count()):
        path = _slot_dir() / f"{index}.lock"
        try:
            with FileLock(path, timeout=0.01):
                pass
        except (TimeoutError, OSError):
            taken += 1
    return taken


@contextmanager
def engine_slot(
    what: str = "engine work",
    timeout: float = 300.0,
    on_wait=None,
) -> Iterator[None]:
    """Hold one of N slots for the duration of the block.

    Slots are tried in RANDOM order. Scanning from zero every time makes
    every process queue behind the same slot and turns N slots into one;
    randomising spreads them with no coordination.

    Backoff is exponential WITH jitter, for the same reason: processes that
    started together would otherwise retry together forever.
    """
    deadline = time.monotonic() + timeout
    delay = 0.05
    announced = False
    order = list(range(slot_count()))
    random.shuffle(order)

    while True:
        for index in order:
            try:
                with FileLock(_slot_dir() / f"{index}.lock", timeout=0.01):
                    yield
                    return
            except (TimeoutError, OSError):
                continue

        if time.monotonic() >= deadline:
            holders = active_holders()
            raise errors.SocketBusyError(
                f"The container engine is busy: every one of {slot_count()} "
                f"slots is held, and waiting {timeout:g}s for one did not "
                f"free it. {holders} HyperBox operation(s) are active.",
                fix="Wait for the other work to finish, or raise the bound "
                    "with HYPERBOX_ENGINE_SLOTS.",
                context={"waiting_for": what, "slots": slot_count()},
            )

        if on_wait is not None and not announced:
            announced = True
            on_wait(active_holders())
        time.sleep(delay + random.uniform(0, delay))
        delay = min(delay * 2, 2.0)
        random.shuffle(order)
