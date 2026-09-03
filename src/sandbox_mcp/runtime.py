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
    by id; it never holds a backend-specific object directly."""

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

    def create(self, language: str, backend: str) -> SandboxHandle:
        """Create and open a persistent sandbox. Raises on unsupported
        language/backend BEFORE allocating anything."""
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
        """Tear down the sandbox. Idempotent — destroying an already-gone
        sandbox is a no-op success, not an error."""
        ...
