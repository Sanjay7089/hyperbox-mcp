"""The Runtime interface — this project's actual IP.

Everything above this line (the MCP tools, the session registry, the
policy) talks to a `Runtime`, never to llm-sandbox directly. llm-sandbox
is ONE implementation of this protocol, living in
`llm_sandbox_runtime.py`. If it's ever abandoned, too limiting, or we
want a Firecracker/microVM backend later, we write a new class that
satisfies this protocol and change one line of wiring — the MCP layer
never notices.

Do not import llm_sandbox anywhere except in a concrete Runtime
implementation. That rule is what keeps the backend replaceable; a
`from llm_sandbox import ...` in server.py or the session registry is a
bug, not a shortcut.
"""

from __future__ import annotations

from pathlib import Path

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ExecResult:
    """Structured result of one code execution. Never a bare string —
    the calling agent has to be able to reason about what happened, so
    every field is explicit."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class SandboxHandle:
    """An opaque reference to a live sandbox. The MCP layer holds these
    by id; it never holds a backend-specific object directly.

    `meta` is the backend's own reattach payload, opaque above this
    line. The llm-sandbox implementation stores {"container_ref": "..."}
    there; a Firecracker one would store something else entirely. The
    registry persists it verbatim and never interprets it, which is what
    lets a NEW server process rebuild a working handle for a sandbox it
    did not create.
    """

    sandbox_id: str
    language: str
    backend: str
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Runtime(Protocol):
    """A disposable code-execution environment, backend-agnostic.

    A concrete Runtime wraps exactly one backing technology (llm-sandbox
    today; possibly Firecracker or a raw podman-py wrapper tomorrow) and
    exposes only these four operations. Nothing backend-specific leaks
    through this interface — no llm-sandbox types, no docker-py objects.
    """

    def supported_languages(self) -> tuple[str, ...]:
        """Languages this runtime can actually deliver.

        Asked of the runtime rather than read from a constant, because the
        answer differs between implementations and an entry in it is a
        promise made to a caller. A fixed list would either understate what
        the native runtime can do or promise, on the llm-sandbox one,
        languages it cannot run.
        """
        ...

    def create(
        self, language: str, backend: str, sandbox_id: str,
        environment: str | None = None,
        packages: list[str] | None = None,
        sync_in_dir: "Path | None" = None,
    ) -> SandboxHandle:
        """Create and open a persistent sandbox under the given id.

        The id is supplied by the caller, not minted here, so the caller
        can record its intent to create BEFORE a container exists. That
        ordering is what stops a concurrent garbage collection from
        reclaiming a container whose registration has not landed yet.
        The id must be baked into the container's labels; `gc` matches
        on them.

        When `environment` is given, the sandbox is built on that
        environment's image instead of the language default. The
        environment must already exist; a backend never builds one.

        Raises on an unsupported language/backend BEFORE allocating
        anything, and must not return a handle for a sandbox whose
        resource limits the engine did not actually apply.
        """
        ...

    def run(
        self,
        handle: SandboxHandle,
        code: str,
        libraries: list[str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Run code inside an existing sandbox, returning a structured
        result. Safe to call many times on the same handle — that's the
        point of a persistent sandbox."""
        ...

    def destroy(self, handle: SandboxHandle) -> None:
        """Tear down the sandbox.

        Returns normally ONLY when the container is confirmed gone —
        either removed by this call, or reported absent by the engine
        itself. Destroying an already-gone sandbox is a success.

        Raises when the engine cannot be reached, because "I could not
        ask" is not "it is gone". Returning success there is how
        orphaned containers accumulate: the caller drops its record while
        the container keeps running.
        """
        ...

    def alive(self, handle: SandboxHandle) -> bool:
        """Whether the sandbox's container exists and runs right now —
        asked of the engine, not of in-process bookkeeping.

        Raises rather than returning False when the engine cannot be
        reached. False must mean the engine answered and said no.
        """
        ...

    def gc(self, known_ids: set[str]) -> list[str]:
        """Destroy containers this project created whose sandbox_id is
        not in `known_ids`, returning the ids reclaimed.

        Only ever touches containers carrying our own labels. A container
        we did not create is never a candidate, no matter how orphaned it
        looks — that judgement is not ours to make. A container younger
        than the grace period is also never a candidate: another process
        may be creating it right now.
        """
        ...
