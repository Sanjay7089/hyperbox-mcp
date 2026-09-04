"""sandbox-mcp: give any MCP client a disposable, persistent computer to
run code in — create it once, run in it repeatedly, destroy it when done.

Run with: uv run python -m sandbox_mcp.server

The three tools below talk ONLY to a Runtime (see runtime.py). They must
never import llm-sandbox directly — that's what keeps the execution
backend replaceable. See CLAUDE.md and REQUIREMENTS.md before changing
anything here.
"""

from __future__ import annotations

import importlib.util

from fastmcp import FastMCP

from sandbox_mcp.llm_sandbox_runtime import (
    LLMSandboxRuntime,
    SandboxRuntimeError,
    UnsupportedBackendError,
    UnsupportedLanguageError,
)
from sandbox_mcp.registry import Registry
from sandbox_mcp.runtime import Runtime, SandboxHandle

mcp = FastMCP("sandbox-mcp")

# The one place a concrete backend is chosen. Swap this line to change
# execution engines; nothing below it knows or cares which Runtime it is.
_runtime: Runtime = LLMSandboxRuntime()

# Sandbox ownership lives in a durable registry shared by every server
# process, NOT in this process's memory. That is what lets a restarted
# server, or a second server the client launched, keep using a sandbox
# it did not create. See REQUIREMENTS.md Phase 5.
_registry = Registry()

# Server policy. An agent cannot raise these; see REQUIREMENTS.md Phase 6.
MAX_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_CHARS = 20_000


def _cap_output(text: str) -> str:
    """Bound a single stream so one run cannot fill the caller's context.
    Truncation is always marked — silently shortened output would make an
    agent reason about evidence it cannot see."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    dropped = len(text) - MAX_OUTPUT_CHARS
    return (
        text[:MAX_OUTPUT_CHARS]
        + f"\n...[truncated {dropped} chars of {len(text)}; "
        "server cap is {MAX_OUTPUT_CHARS}]".format(MAX_OUTPUT_CHARS=MAX_OUTPUT_CHARS)
    )


def _handle_for(sandbox_id: str) -> SandboxHandle | None:
    """Rebuild a handle from durable state. The container_ref stored in
    `meta` is what the runtime reattaches to."""
    rec = _registry.get(sandbox_id)
    if rec is None:
        return None
    return SandboxHandle(
        sandbox_id=rec.sandbox_id,
        language=rec.language,
        backend=rec.backend,
        meta={"container_ref": rec.container_ref},
    )


def collect_garbage() -> list[str]:
    """Reclaim containers we created that the registry no longer knows
    about, plus any whose inactivity TTL has passed. Only ever touches
    containers carrying our labels."""
    for rec in _registry.expired_records():
        handle = _handle_for(rec.sandbox_id)
        if handle is not None:
            try:
                _runtime.destroy(handle)
            except Exception:  # noqa: BLE001 - best effort reclamation
                pass
        _registry.remove(rec.sandbox_id)
    known = {r.sandbox_id for r in _registry.all_records()}
    try:
        return _runtime.gc(known)
    except Exception:  # noqa: BLE001 - never let GC break startup
        return []


@mcp.tool()
def create_sandbox(language: str = "python", backend: str = "docker") -> dict:
    """Create a persistent, disposable sandbox and return its id.

    Call this once, then call `run` against the returned sandbox_id as
    many times as you need, then `destroy_sandbox` when finished.

    language: "python". backend: "docker". Those are the combinations
    verified against a real container; others are added only once they
    pass the same bar. Invalid values fail here, before anything is
    allocated.

    The sandbox persists: its filesystem and any packages installed via
    `run(libraries=...)` survive between calls. Interpreter memory is
    NOT guaranteed to — write state to a file rather than expecting
    variables to carry over.
    """
    try:
        handle = _runtime.create(language=language, backend=backend)
    except (UnsupportedLanguageError, UnsupportedBackendError) as exc:
        return {"error": str(exc)}
    except SandboxRuntimeError as exc:
        return {"error": f"Failed to create sandbox: {exc}"}
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        return {"error": f"Failed to create sandbox: {type(exc).__name__}: {exc}"}
    _registry.add(
        sandbox_id=handle.sandbox_id,
        container_ref=handle.meta.get("container_ref", ""),
        language=handle.language,
        backend=handle.backend,
    )
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

    Safe to call repeatedly on the same sandbox_id. The sandbox's
    filesystem and installed packages persist between calls; in-memory
    variables are NOT guaranteed to survive, so persist anything you
    need to a file.

    The sandbox has NO network access while your code runs. Passing
    `libraries` installs them in a brief, separate network-enabled step
    before your code executes sealed. `timeout` is capped by the server
    and may not be null.
    """
    if timeout is None:
        return {
            "error": (
                "timeout=None is not permitted; execution time is server "
                f"policy. Pass a number up to {MAX_TIMEOUT_SECONDS:g} seconds."
            )
        }
    timeout = min(float(timeout), MAX_TIMEOUT_SECONDS)

    handle = _handle_for(sandbox_id)
    if handle is None:
        return {"error": f"No sandbox '{sandbox_id}'. Create one first."}
    try:
        # Held for the whole call so two processes cannot drive the same
        # container's session concurrently.
        with _registry.lock(sandbox_id):
            result = _runtime.run(
                handle, code=code, libraries=libraries, timeout=timeout
            )
            _registry.touch(sandbox_id)
    except SandboxRuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # REQUIREMENTS.md: results are ALWAYS structured. An unexpected
        # backend exception must reach the agent as something it can
        # reason about, not as a crashed tool call.
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "stdout": _cap_output(result.stdout),
        "stderr": _cap_output(result.stderr),
        "exit_code": result.exit_code,
        "success": result.success,
    }


@mcp.tool()
def destroy_sandbox(sandbox_id: str) -> dict:
    """Tear down a sandbox. Idempotent — destroying one that's already
    gone is a success, not an error."""
    handle = _handle_for(sandbox_id)
    if handle is None:
        # Idempotent, so this is a success. It reports an affirmative
        # status rather than a boolean: `destroyed: false` on a call
        # that worked invites an agent to retry or error-handle it.
        return {"sandbox_id": sandbox_id, "status": "already_gone"}
    try:
        with _registry.lock(sandbox_id):
            _runtime.destroy(handle)
            # `already_gone` must describe the CONTAINER, not our
            # bookkeeping. Confirm with the engine before saying so.
            if _runtime.alive(handle):
                return {
                    "error": (
                        f"Sandbox '{sandbox_id}' container is still running "
                        "after destroy was attempted."
                    )
                }
            _registry.remove(sandbox_id)
    except SandboxRuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"sandbox_id": sandbox_id, "status": "destroyed"}


# --- Phase 3 seam (mcp-composer's scope) ------------------------------
# External MCP servers — a codebase indexer under prefix `index`,
# Context7 under `docs` — are mounted by `sandbox_mcp/mounts.py`, which
# owns that wiring end to end (see REQUIREMENTS.md Phase 3). That module
# does not exist until Phase 3 lands, which is why this is a find_spec
# check and not a try/except ImportError: an absent module is expected,
# but a module that exists and fails to import is a real error and must
# surface loudly rather than being swallowed into a silent no-op.
#
# The contract is one function: `mounts.register(mcp)`. Nothing else in
# this file changes for Phase 3 — keeping the lifecycle tools and the
# mounting work in separate files is what lets the two phases proceed in
# parallel without touching each other's code.
#
# An unmounted capability is simply absent. Never stub one with a fake
# tool that returns placeholder data.
if importlib.util.find_spec("sandbox_mcp.mounts") is not None:
    from sandbox_mcp import mounts

    mounts.register(mcp)


def main() -> None:
    # Reclaim anything left behind by a previous process before serving.
    collect_garbage()
    mcp.run()


if __name__ == "__main__":
    main()
