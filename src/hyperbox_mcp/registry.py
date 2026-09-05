"""Durable sandbox ownership, shared across processes.

Sandbox ownership used to live in a dict inside one Python process. That
loses a sandbox whenever the server restarts or a second server process
is launched, while the container keeps running — measured at a 5/10
failure rate alternating between two live processes, plus orphaned
containers that `destroy_sandbox` cheerfully reported as `already_gone`.

This module is the durable replacement. It stores only opaque strings:
it never imports llm_sandbox, never speaks to a container engine, and
has no idea what a `container_ref` means. That keeps the backend
replaceable — a future runtime stores its own handle string here and
nothing else changes.

State lives OUTSIDE the repository (see `state_dir`), because a checkout
is not a good place for machine state and because two worktrees of the
same repo must not silently share or clobber a registry.

Two invariants make the lifecycle race-safe:

1. A row is written BEFORE its container exists, in state `creating`.
   Garbage collection ignores `creating` rows, so a container cannot be
   reclaimed in the window between "engine made it" and "we recorded
   it".
2. Every state-changing operation happens under `lock(sandbox_id)`, and
   re-reads the row inside the lock. A decision made on a row read
   outside the lock is stale by the time it is acted on.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from hyperbox_mcp.policy import DEFAULT_TTL_SECONDS

STATE_CREATING = "creating"
STATE_READY = "ready"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    sandbox_id    TEXT PRIMARY KEY,
    container_ref TEXT NOT NULL,
    language      TEXT NOT NULL,
    backend       TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL NOT NULL,
    expires_at    REAL NOT NULL,
    state         TEXT NOT NULL DEFAULT 'ready'
);
"""


@dataclass(frozen=True)
class SandboxRecord:
    sandbox_id: str
    container_ref: str
    language: str
    backend: str
    created_at: float
    last_used_at: float
    expires_at: float
    state: str = STATE_READY

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def ready(self) -> bool:
        return self.state == STATE_READY


def state_dir() -> Path:
    """Where registry state lives. Never inside the repo."""
    override = os.environ.get("HYPERBOX_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "hyperbox-mcp"
    return Path.home() / ".local" / "state" / "hyperbox-mcp"


class Registry:
    """SQLite-backed sandbox registry, safe for concurrent processes.

    WAL mode plus a busy timeout lets several server processes read and
    write without stepping on each other; `lock()` gives callers genuine
    mutual exclusion for the duration of a container operation.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or state_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "registry.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL so a reader never blocks a writer across processes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # A registry written before `state` existed still has live
            # sandboxes in it; adopt them as ready rather than orphaning
            # their containers.
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(sandboxes)").fetchall()
            }
            if "state" not in columns:
                conn.execute(
                    "ALTER TABLE sandboxes ADD COLUMN state TEXT NOT NULL "
                    f"DEFAULT '{STATE_READY}'"
                )

    # --- per-sandbox mutual exclusion ---------------------------------

    @contextmanager
    def lock(self, sandbox_id: str) -> Iterator[None]:
        """Exclusive lock for one sandbox, held across processes.

        A separate lock file per sandbox, so work on one sandbox never
        blocks another. flock is released by the OS if the holder dies,
        which matters here: a killed server must not wedge a sandbox
        permanently.

        Ids are validated upstream, but never trust one straight into a
        path.
        """
        lock_dir = self.dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in sandbox_id if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError(f"Unusable sandbox id for locking: {sandbox_id!r}")
        lock_path = lock_dir / f"{safe}.lock"
        fh = lock_path.open("w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    # --- records ------------------------------------------------------

    def reserve(
        self,
        sandbox_id: str,
        language: str,
        backend: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> SandboxRecord:
        """Claim an id BEFORE the container exists.

        The row is written in state `creating` with an empty
        container_ref. Garbage collection skips it, so nothing can
        reclaim the container that is about to be created under this id.
        """
        now = time.time()
        rec = SandboxRecord(
            sandbox_id=sandbox_id,
            container_ref="",
            language=language,
            backend=backend,
            created_at=now,
            last_used_at=now,
            expires_at=now + ttl_seconds,
            state=STATE_CREATING,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sandboxes VALUES (?,?,?,?,?,?,?,?)",
                (
                    rec.sandbox_id,
                    rec.container_ref,
                    rec.language,
                    rec.backend,
                    rec.created_at,
                    rec.last_used_at,
                    rec.expires_at,
                    rec.state,
                ),
            )
        return rec

    def finalize(
        self,
        sandbox_id: str,
        container_ref: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Promote a reservation to a usable sandbox."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sandboxes SET container_ref = ?, state = ?, "
                "last_used_at = ?, expires_at = ? WHERE sandbox_id = ?",
                (container_ref, STATE_READY, now, now + ttl_seconds, sandbox_id),
            )

    def get(self, sandbox_id: str) -> SandboxRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE sandbox_id = ?", (sandbox_id,)
            ).fetchone()
        return SandboxRecord(**dict(row)) if row else None

    def get_ready(self, sandbox_id: str) -> SandboxRecord | None:
        """A usable sandbox only. A reservation still being created is
        not something a caller may run code in."""
        rec = self.get(sandbox_id)
        return rec if rec is not None and rec.ready else None

    def touch(self, sandbox_id: str, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        """Mark a sandbox used now and push its expiry out. TTL is
        inactivity-based, so an actively used sandbox is never reclaimed."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sandboxes SET last_used_at = ?, expires_at = ? "
                "WHERE sandbox_id = ?",
                (now, now + ttl_seconds, sandbox_id),
            )

    def remove(self, sandbox_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sandboxes WHERE sandbox_id = ?", (sandbox_id,))

    def all_records(self) -> list[SandboxRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sandboxes").fetchall()
        return [SandboxRecord(**dict(r)) for r in rows]

    def known_ids(self) -> set[str]:
        """Every id the registry is accountable for, reservations
        included. This is what garbage collection must not touch."""
        with self._connect() as conn:
            rows = conn.execute("SELECT sandbox_id FROM sandboxes").fetchall()
        return {r["sandbox_id"] for r in rows}

    def expired_records(self) -> list[SandboxRecord]:
        """Ready sandboxes past their inactivity TTL.

        Reservations are excluded: a `creating` row is young by
        definition and its container may not exist yet.
        """
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE expires_at <= ? AND state = ?",
                (now, STATE_READY),
            ).fetchall()
        return [SandboxRecord(**dict(r)) for r in rows]
