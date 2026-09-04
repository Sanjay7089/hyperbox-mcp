"""Durable sandbox ownership, shared across processes.

Sandbox ownership used to live in a dict inside one Python process. That
loses a sandbox whenever the server restarts or a second server process
is launched, while the container keeps running — measured at a 5/10
failure rate alternating between two live processes, plus orphaned
containers that `destroy_sandbox` cheerfully reported as `already_gone`.

This module is the durable replacement. It stores only opaque strings:
it never imports llm_sandbox, never speaks to a container engine, and
has no idea what a `container_ref` means. That keeps the backend
replaceable — a future Firecracker runtime stores its own handle string
here and nothing else changes.

State lives OUTSIDE the repository (see `state_dir`), because a checkout
is not a good place for machine state and because two worktrees of the
same repo must not silently share or clobber a registry.
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

# Inactivity TTL. A sandbox untouched for this long is reclaimable by the
# garbage collector; see REQUIREMENTS.md Phase 5.
DEFAULT_TTL_SECONDS = float(os.environ.get("HYPERBOX_MCP_TTL_SECONDS", 30 * 60))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    sandbox_id    TEXT PRIMARY KEY,
    container_ref TEXT NOT NULL,
    language      TEXT NOT NULL,
    backend       TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL NOT NULL,
    expires_at    REAL NOT NULL
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

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


def state_dir() -> Path:
    """Where registry state lives. Never inside the repo."""
    override = os.environ.get("HYPERBOX_MCP_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "hyperbox-mcp"
    return Path.home() / ".local" / "state" / "hyperbox-mcp"


class Registry:
    """SQLite-backed sandbox registry, safe for concurrent processes.

    WAL mode plus a short busy timeout lets several server processes read
    and write without stepping on each other; `lock()` gives callers
    genuine mutual exclusion for the duration of a container operation.
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

    # --- per-sandbox mutual exclusion ---------------------------------

    @contextmanager
    def lock(self, sandbox_id: str) -> Iterator[None]:
        """Exclusive lock for one sandbox, held across processes.

        A separate lock file per sandbox, so work on one sandbox never
        blocks another. flock is released by the OS if the holder dies,
        which matters here: a killed server must not wedge a sandbox
        permanently.
        """
        lock_dir = self.dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        # sandbox ids are generated hex, but never trust an id straight
        # into a path.
        safe = "".join(ch for ch in sandbox_id if ch.isalnum() or ch in "-_")
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

    def add(
        self,
        sandbox_id: str,
        container_ref: str,
        language: str,
        backend: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> SandboxRecord:
        now = time.time()
        rec = SandboxRecord(
            sandbox_id=sandbox_id,
            container_ref=container_ref,
            language=language,
            backend=backend,
            created_at=now,
            last_used_at=now,
            expires_at=now + ttl_seconds,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sandboxes VALUES (?,?,?,?,?,?,?)",
                (
                    rec.sandbox_id,
                    rec.container_ref,
                    rec.language,
                    rec.backend,
                    rec.created_at,
                    rec.last_used_at,
                    rec.expires_at,
                ),
            )
        return rec

    def get(self, sandbox_id: str) -> SandboxRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE sandbox_id = ?", (sandbox_id,)
            ).fetchone()
        return SandboxRecord(**dict(row)) if row else None

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

    def expired_records(self) -> list[SandboxRecord]:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE expires_at <= ?", (now,)
            ).fetchall()
        return [SandboxRecord(**dict(r)) for r in rows]
