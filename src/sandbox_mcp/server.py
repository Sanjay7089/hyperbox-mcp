"""sandbox-mcp: give any MCP client a disposable, persistent computer to
run code in — create it once, run in it repeatedly, destroy it when done.

Run with: python -m sandbox_mcp.server

The three tools below talk ONLY to a Runtime (see runtime.py). They must
never import llm-sandbox directly — that's what keeps the execution
backend replaceable. See CLAUDE.md and REQUIREMENTS.md before changing
anything here.
"""

from __future__ import annotations

from fastmcp import FastMCP

from sandbox_mcp.llm_sandbox_runtime import (
    LLMSandboxRuntime,
    SandboxRuntimeError,
    UnsupportedBackendError,
    UnsupportedLanguageError,
)
from sandbox_mcp.runtime import Runtime, SandboxHandle

mcp = FastMCP("sandbox-mcp")

# The one place a concrete backend is chosen. Swap this line to change
# execution engines; nothing below it knows or cares which Runtime it is.
_runtime: Runtime = LLMSandboxRuntime()

# Live handles by id, so `run`/`destroy` can find the sandbox `create`
# made. In-process only — a sandbox does not survive a server restart.
_handles: dict[str, SandboxHandle] = {}


@mcp.tool()
def create_sandbox(language: str = "python", backend: str = "docker") -> dict:
    """Create a persistent, disposable sandbox and return its id.

    Call this once, then call `run` against the returned sandbox_id as
    many times as you need, then `destroy_sandbox` when finished.

    language: "python" or "javascript" (v1). backend: "docker" or
    "podman", both rootless. Invalid values fail here, before anything
    is allocated.
    """
    try:
        handle = _runtime.create(language=language, backend=backend)
    except (UnsupportedLanguageError, UnsupportedBackendError) as exc:
        return {"error": str(exc)}
    except SandboxRuntimeError as exc:
        return {"error": f"Failed to create sandbox: {exc}"}
    _handles[handle.sandbox_id] = handle
    return {
        "sandbox_id": handle.sandbox_id,
        "language": handle.language,
        "backend": handle.backend,
    }


@mcp.tool()
def run(
    sandbox_id: str,
    code: str,
    libraries: list[str] | None = None,
    timeout: float | None = 30,
) -> dict:
    """Run code inside an existing sandbox. Returns structured output:
    stdout, stderr, exit_code, success — never a bare 'it failed' string,
    so the agent can reason about exactly what happened and fix it.

    Safe to call repeatedly on the same sandbox_id; state persists
    between calls until the sandbox is destroyed.
    """
    handle = _handles.get(sandbox_id)
    if handle is None:
        return {"error": f"No sandbox '{sandbox_id}'. Create one first."}
    try:
        result = _runtime.run(
            handle, code=code, libraries=libraries, timeout=timeout
        )
    except SandboxRuntimeError as exc:
        return {"error": str(exc)}
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "success": result.success,
    }


@mcp.tool()
def destroy_sandbox(sandbox_id: str) -> dict:
    """Tear down a sandbox. Idempotent — destroying one that's already
    gone is a success, not an error."""
    handle = _handles.pop(sandbox_id, None)
    if handle is None:
        return {"sandbox_id": sandbox_id, "destroyed": False, "note": "already gone"}
    try:
        _runtime.destroy(handle)
    except SandboxRuntimeError as exc:
        return {"error": str(exc)}
    return {"sandbox_id": sandbox_id, "destroyed": True}


# --- Phase 3 (mcp-composer's scope) -----------------------------------
# Mount external MCP servers here once available, e.g.:
#   idx = FastMCP.as_proxy("<codebase indexer connection>")
#   mcp.mount(idx, prefix="index")
#   docs = FastMCP.as_proxy("<Context7 connection>")
#   mcp.mount(docs, prefix="docs")
# Left unmounted for now — see REQUIREMENTS.md Phase 3. Do not stub
# these with fake tools; an unmounted capability is simply absent.


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
