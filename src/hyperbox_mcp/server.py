"""HyperBox: a container an AI agent can run code in without risking your machine.

Run with: uv run python -m hyperbox_mcp.server

The tools below talk ONLY to a Runtime (see runtime.py). They must never
import llm-sandbox directly — that is what keeps the execution backend
replaceable.

A note on why this file is mostly descriptions and annotations: an MCP
client has no dispatcher. It decides whether to call a tool purely from
that tool's name, description and annotations. Measured here: a working
search tool described only as "search the codebase" was REFUSED by a
client that then asked the user to upload files by hand; naming what it
covered turned the same tool into a correct answer. So the prose in this
file is not documentation, it is the routing logic.
"""

from __future__ import annotations

import asyncio
import json

from fastmcp import Context, FastMCP

from hyperbox_mcp.llm_sandbox_runtime import (
    MEM_LIMIT,
    NANO_CPUS,
    PIDS_LIMIT,
    LLMSandboxRuntime,
    SandboxRuntimeError,
    UnsupportedBackendError,
    UnsupportedLanguageError,
    _BACKENDS,
    _LANGUAGES,
)
from hyperbox_mcp.registry import DEFAULT_TTL_SECONDS, Registry
from hyperbox_mcp.runtime import Runtime, SandboxHandle

mcp = FastMCP("HyperBox")

# The one place a concrete backend is chosen. Swap this line to change
# execution engines; nothing below it knows or cares which Runtime it is.
_runtime: Runtime = LLMSandboxRuntime()

# Sandbox ownership lives in a durable registry shared by every server
# process, NOT in this process's memory. That is what lets a restarted
# server, or a second server the client launched, keep using a sandbox it
# did not create.
_registry = Registry()

# Server policy. An agent cannot raise these.
MAX_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_CHARS = 20_000

_ENGINE_HINT = (
    "Check the container engine is running: `docker info`, or `podman info` "
    "(on macOS, `podman machine start` and export CONTAINER_HOST)."
)


def _cap_output(text: str) -> str:
    """Bound a single stream so one run cannot fill the caller's context.
    Truncation is always marked — silently shortened output would make an
    agent reason about evidence it cannot see."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    dropped = len(text) - MAX_OUTPUT_CHARS
    return (
        f"{text[:MAX_OUTPUT_CHARS]}\n"
        f"...[truncated {dropped} chars of {len(text)}; "
        f"server cap is {MAX_OUTPUT_CHARS}]"
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


# --- what the sandbox actually is, readable before committing to it -----


@mcp.resource(
    "hyperbox://capabilities",
    name="HyperBox sandbox capabilities",
    description=(
        "Exactly what a HyperBox sandbox provides and enforces: languages, "
        "backends, memory/CPU/process ceilings, network posture, and what "
        "does and does not persist between runs. Read this before planning "
        "work that might exceed a limit."
    ),
    mime_type="application/json",
)
def capabilities() -> str:
    """Discoverable limits. Without this an agent only learns the ceilings
    by hitting them, which costs a failed run to find out."""
    return json.dumps(
        {
            "languages": sorted(_LANGUAGES),
            "backends": sorted(_BACKENDS),
            "limits": {
                "memory": MEM_LIMIT,
                "cpus": NANO_CPUS / 1_000_000_000,
                "max_processes": PIDS_LIMIT,
                "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
                "max_output_chars_per_stream": MAX_OUTPUT_CHARS,
                "inactivity_ttl_seconds": DEFAULT_TTL_SECONDS,
            },
            "network": {
                "while_your_code_runs": "disabled",
                "during_library_install": "enabled briefly, then re-sealed",
            },
            "isolation": {
                "host_filesystem": "not mounted",
                "container_engine_socket": "not mounted",
            },
            "persistence": {
                "filesystem": "persists across run() calls in one sandbox",
                "installed_packages": "persist across run() calls",
                "interpreter_memory": "NOT guaranteed; each run is a fresh process",
            },
        },
        indent=2,
    )


# --- tools ---------------------------------------------------------------


@mcp.tool(
    annotations={
        "title": "Create an isolated sandbox",
        "readOnlyHint": False,
        # Creates a container; destroys nothing.
        "destructiveHint": False,
        # Each call yields a NEW sandbox — calling twice is not the same as once.
        "idempotentHint": False,
        # May pull a container image from a registry on first use.
        "openWorldHint": True,
    }
)
async def create_sandbox(
    ctx: Context, language: str = "python", backend: str = "docker"
) -> dict:
    """Create a disposable container to run untrusted or unverified code in.

    USE THIS BEFORE RUNNING CODE YOU WROTE OR THE USER HAS NOT REVIEWED —
    especially anything that writes files, installs packages, deletes
    things, or that you cannot predict the behaviour of. Code inside a
    HyperBox sandbox cannot read or write the user's filesystem, cannot
    reach their network, and cannot touch the container engine.

    Do NOT use it when: the user explicitly asked to run something on their
    own machine; you only need to read a file (use your own file tools);
    or the command's whole purpose is to change the user's project.

    Then call `run` against the returned sandbox_id as many times as you
    need, and `destroy_sandbox` when finished. First use of a language may
    take a minute while its image is pulled.

    Read the `hyperbox://capabilities` resource for exact limits.
    """
    # llm-sandbox does not surface image-pull progress, so this is a
    # start/finish signal rather than a percentage — enough for a client to
    # show that a possibly-slow pull is underway instead of appearing hung.
    # (ctx.info is deliberately not used: MCP deprecated the logging
    # capability in SEP-2577, 2026-07-28.)
    await ctx.report_progress(0, 100, f"pulling image / starting {language} sandbox")
    try:
        # Offloaded: container creation blocks for seconds to minutes, and
        # must not stall the server's event loop.
        handle = await asyncio.to_thread(
            _runtime.create, language=language, backend=backend
        )
    except (UnsupportedLanguageError, UnsupportedBackendError) as exc:
        return {"error": str(exc)}
    except SandboxRuntimeError as exc:
        return {"error": f"Failed to create sandbox: {exc}. {_ENGINE_HINT}"}
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        return {
            "error": (
                f"Failed to create sandbox: {type(exc).__name__}: {exc}. "
                f"{_ENGINE_HINT}"
            )
        }
    _registry.add(
        sandbox_id=handle.sandbox_id,
        container_ref=handle.meta.get("container_ref", ""),
        language=handle.language,
        backend=handle.backend,
    )
    await ctx.report_progress(100, 100, "sandbox ready")
    return {
        "sandbox_id": handle.sandbox_id,
        "language": handle.language,
        "backend": handle.backend,
        "next": (
            f"Call run(sandbox_id='{handle.sandbox_id}', code=...) to execute. "
            "Call destroy_sandbox when done."
        ),
    }


@mcp.tool(
    annotations={
        "title": "Run code inside the sandbox",
        "readOnlyHint": False,
        # THE point of this server: executing here cannot harm the host.
        # Destructive only within a container that exists to be thrown away.
        "destructiveHint": False,
        "idempotentHint": False,
        # Your code runs sealed, but a declared `libraries` install does
        # reach a package registry. Annotated conservatively.
        "openWorldHint": True,
    }
)
def run(
    sandbox_id: str,
    code: str,
    libraries: list[str] | None = None,
    timeout: float | None = 30,
) -> dict:
    """Execute code in a sandbox and return exactly what happened.

    Returns {stdout, stderr, exit_code, success} — never a bare "it
    failed", so you can read the real traceback and fix the actual cause.
    Prefer this over running generated code on the user's machine.

    Safe to call repeatedly on one sandbox_id. The filesystem and installed
    packages persist between calls; in-memory variables do NOT — each run
    is a fresh process, so write anything you need to keep to a file.

    Your code runs with NO network access. Passing `libraries` installs
    them in a brief, separate network-enabled step first, then re-seals
    before your code executes. `timeout` is capped by the server and may
    not be null. Output is truncated past a limit, and marked when it is.
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
        return {
            "error": (
                f"No sandbox '{sandbox_id}'. Call create_sandbox first, then "
                "use the sandbox_id it returns."
            )
        }
    try:
        # Held for the whole call so two processes cannot drive the same
        # container's session concurrently.
        with _registry.lock(sandbox_id):
            result = _runtime.run(
                handle, code=code, libraries=libraries, timeout=timeout
            )
            _registry.touch(sandbox_id)
    except SandboxRuntimeError as exc:
        return {"error": f"{exc}. {_ENGINE_HINT}"}
    except Exception as exc:  # noqa: BLE001
        # Results are ALWAYS structured. An unexpected backend exception
        # must reach the agent as something it can reason about, not as a
        # crashed tool call.
        return {"error": f"{type(exc).__name__}: {exc}. {_ENGINE_HINT}"}
    return {
        "stdout": _cap_output(result.stdout),
        "stderr": _cap_output(result.stderr),
        "exit_code": result.exit_code,
        "success": result.success,
    }


@mcp.tool(
    annotations={
        "title": "Destroy the sandbox",
        "readOnlyHint": False,
        # Genuinely destructive — but only ever to the sandbox, never the host.
        "destructiveHint": True,
        # Destroying an already-gone sandbox succeeds; verified by the suite.
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def destroy_sandbox(sandbox_id: str) -> dict:
    """Tear down a sandbox and free its resources.

    Idempotent — destroying one that is already gone is a success, not an
    error, and reports status "already_gone". Call this when finished;
    abandoned sandboxes are also reclaimed automatically after an
    inactivity timeout.
    """
    handle = _handle_for(sandbox_id)
    if handle is None:
        # Idempotent, so this is a success. It reports an affirmative
        # status rather than a boolean: `destroyed: false` on a call that
        # worked invites an agent to retry or error-handle it.
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
                        f"after destroy was attempted. {_ENGINE_HINT}"
                    )
                }
            _registry.remove(sandbox_id)
    except SandboxRuntimeError as exc:
        return {"error": f"{exc}. {_ENGINE_HINT}"}
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        return {"error": f"{type(exc).__name__}: {exc}. {_ENGINE_HINT}"}
    return {"sandbox_id": sandbox_id, "status": "destroyed"}


# --- make the safe path the easy path ------------------------------------


@mcp.prompt(
    name="run_safely",
    description=(
        "Execute code the user has not reviewed, inside a sandbox, and "
        "report what actually happened rather than what should happen."
    ),
)
def run_safely(code: str, language: str = "python") -> str:
    """A prompt, not a tool: the user invokes it to opt into the safe path."""
    return (
        f"Run the following {language} code in a HyperBox sandbox rather than "
        f"on this machine, because it has not been reviewed.\n\n"
        f"1. create_sandbox(language='{language}')\n"
        f"2. run(...) with the code below\n"
        f"3. Report the ACTUAL stdout, stderr and exit_code you received. If "
        f"it failed, read the real traceback, fix the cause, and run again.\n"
        f"4. destroy_sandbox when finished.\n\n"
        f"Do not claim it works unless you have seen a successful run.\n\n"
        f"```{language}\n{code}\n```"
    )


def main() -> None:
    # Reclaim anything left behind by a previous process before serving.
    collect_garbage()
    mcp.run()


if __name__ == "__main__":
    main()
