"""HyperBox: a container an agent can run code in without risking your machine.

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

Every tool body follows one order: validate, then lock, then re-read
state inside the lock, then act. A decision made on a record read before
the lock was taken is already stale — that is the shape of every
lifecycle race this server has had.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager, suppress

from fastmcp import Context, FastMCP

from hyperbox_mcp import engine, policy, validate
from hyperbox_mcp.engine import EngineUnavailableError
from hyperbox_mcp.llm_sandbox_runtime import (
    LLMSandboxRuntime,
    SandboxRuntimeError,
    UnsupportedBackendError,
    UnsupportedEnvironmentError,
    UnsupportedLanguageError,
    image_for,
)
from hyperbox_mcp.registry import Registry
from hyperbox_mcp.runtime import Runtime, SandboxHandle
from hyperbox_mcp.validate import InvalidInput

mcp = FastMCP("HyperBox")

# The one place a concrete backend is chosen. Swap this line to change
# execution engines; nothing below it knows or cares which Runtime it is.
_runtime: Runtime = LLMSandboxRuntime()

# Sandbox ownership lives in a durable registry shared by every server
# process, NOT in this process's memory. That is what lets a restarted
# server, or a second server the client launched, keep using a sandbox it
# did not create.
_registry = Registry()


def _cap_output(text: str) -> str:
    """Bound a single stream so one run cannot fill the caller's context.
    Truncation is always marked — silently shortened output would make an
    agent reason about evidence it cannot see."""
    if len(text) <= policy.MAX_OUTPUT_CHARS:
        return text
    dropped = len(text) - policy.MAX_OUTPUT_CHARS
    return (
        f"{text[:policy.MAX_OUTPUT_CHARS]}\n"
        f"...[truncated {dropped} chars of {len(text)}; "
        f"server cap is {policy.MAX_OUTPUT_CHARS}]"
    )


#: How often to tell the client we are still working. MCP clients cut a
#: tool call off after a period of silence — 60 seconds is typical — and
#: creating a sandbox can legitimately take longer than that on a cold
#: machine, because the language image is several gigabytes. Reporting
#: progress well inside that window is what turns "the request timed out"
#: into "this is still downloading".
HEARTBEAT_SECONDS = 8.0

#: Progress is reported out of 100 but the real duration is unknown, so
#: it approaches this ceiling without ever claiming to be finished. Only
#: the actual completion reports 100.
HEARTBEAT_CEILING = 90


@asynccontextmanager
async def _heartbeat(ctx: Context, what: str):
    """Report progress every few seconds until the body finishes.

    The work itself runs in a worker thread and cannot report anything on
    its own — the execution backend surfaces no pull or build progress —
    so this is a liveness signal rather than a measurement. It says "still
    working, here is what on", which is what a client needs to keep the
    call alive and what a person needs to not think it has hung.

    Cancelled on every exit path, success or failure, so a failed create
    never leaves a task reporting progress for work that has stopped.
    """
    done = asyncio.Event()

    async def beat() -> None:
        progress = 10
        try:
            await ctx.report_progress(progress, 100, what)
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=HEARTBEAT_SECONDS)
                    return  # finished before the next beat was due
                except asyncio.TimeoutError:
                    progress = min(progress + 8, HEARTBEAT_CEILING)
                    await ctx.report_progress(progress, 100, f"{what}…")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # A client that cannot receive progress must not break the
            # operation it was reporting on.
            return

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        done.set()
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


def _handle_for(rec) -> SandboxHandle:
    """Rebuild a handle from durable state. The container_ref stored on
    the record is what the runtime reattaches to."""
    return SandboxHandle(
        sandbox_id=rec.sandbox_id,
        language=rec.language,
        backend=rec.backend,
        meta={"container_ref": rec.container_ref},
    )


def _no_sandbox(sandbox_id: str) -> dict:
    return {
        "error": (
            f"No sandbox '{sandbox_id}'. It was never created, was already "
            "destroyed, or was reclaimed after its inactivity timeout. Call "
            "create_sandbox and use the sandbox_id it returns."
        )
    }


def collect_garbage() -> list[str]:
    """Reclaim containers we created that the registry no longer knows
    about, plus any whose inactivity TTL has passed. Only ever touches
    containers carrying our labels.

    Each expiry is re-checked while holding that sandbox's lock: a `run`
    in another process may have touched the record between the query and
    the lock, and an in-use sandbox must never be collected.
    """
    for rec in _registry.expired_records():
        try:
            with _registry.lock(rec.sandbox_id):
                current = _registry.get(rec.sandbox_id)
                if current is None or not current.expired or not current.ready:
                    continue  # revived by a concurrent run, or already gone
                try:
                    _runtime.destroy(_handle_for(current))
                except EngineUnavailableError:
                    # Cannot confirm removal, so keep the row. Reclaiming
                    # it now would forget a container that may still run.
                    continue
                except Exception:  # noqa: BLE001 - best effort reclamation
                    pass
                _registry.remove(rec.sandbox_id)
        except OSError:  # noqa: PERF203 - a lock we cannot take is not fatal
            continue

    # Reservations that never became sandboxes. Dropping the row is
    # enough: the container, if one was ever made, still carries our
    # labels, so the sweep below reclaims it once it is past the creation
    # grace period. Removing the row first is what makes it visible.
    for stale in _registry.stale_reservations():
        try:
            with _registry.lock(stale.sandbox_id):
                current = _registry.get(stale.sandbox_id)
                if current is not None and not current.ready and current.expired:
                    _registry.remove(stale.sandbox_id)
        except OSError:  # noqa: PERF203 - a lock we cannot take is not fatal
            continue

    # Snapshot AFTER the sweep so a sandbox created during it is included.
    # Reservations count as known: their containers are mid-creation.
    try:
        return _runtime.gc(_registry.known_ids())
    except Exception:  # noqa: BLE001 - never let GC break startup
        return []


# --- what the sandbox actually is, readable before committing to it -----


@mcp.resource(
    "hyperbox://capabilities",
    name="HyperBox sandbox capabilities",
    description=(
        "Exactly what a HyperBox sandbox provides and enforces: languages, "
        "backends, memory/CPU/process ceilings, network posture, writable "
        "paths, and what does and does not persist between runs. Read this "
        "before planning work that might exceed a limit."
    ),
    mime_type="application/json",
)
def capabilities() -> str:
    """Discoverable limits. Without this an agent only learns the ceilings
    by hitting them, which costs a failed run to find out."""
    return json.dumps(
        {
            "languages": sorted(policy.LANGUAGES),
            # Resolved per request, so an environment the user built
            # after this server started is listed without a restart.
            "environments": sorted(policy.environments()),
            "backends": sorted(policy.BACKEND_CHOICES),
            "experimental_backends": sorted(policy.EXPERIMENTAL_BACKENDS),
            "limits": {
                "memory": policy.MEM_LIMIT,
                "cpus": policy.NANO_CPUS / 1_000_000_000,
                "max_processes": policy.PIDS_LIMIT,
                "max_timeout_seconds": policy.MAX_TIMEOUT_SECONDS,
                "max_output_chars_per_stream": policy.MAX_OUTPUT_CHARS,
                "max_code_chars": policy.MAX_CODE_CHARS,
                "max_libraries": policy.MAX_LIBRARIES,
                "inactivity_ttl_seconds": policy.DEFAULT_TTL_SECONDS,
            },
            "network": {
                "while_your_code_runs": "disabled",
                "during_library_install": "enabled briefly, then re-sealed",
                "installable": (
                    "named packages from the default index only; flags, "
                    "URLs, paths and VCS references are refused"
                ),
            },
            "filesystem": {
                "host_filesystem": "not mounted",
                "container_engine_socket": "not mounted",
                "scratch_space": sorted(policy.TMPFS_PATHS),
                "scratch_size": policy.TMPFS_SIZE,
                "scratch_note": (
                    "/work is a size-limited tmpfs, counted against the "
                    "memory limit and discarded with the sandbox. Write "
                    "working files there. /tmp is writable too but is not "
                    "separately size-limited."
                ),
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
    ctx: Context,
    language: str = "python",
    backend: str = "auto",
    environment: str | None = None,
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

    Create ONE sandbox per task and reuse it: call `run` against the
    returned sandbox_id as many times as you need, then `destroy_sandbox`
    when the task is done. Do not create a sandbox per snippet, and do not
    carry one across unrelated tasks — files, installed packages and
    scratch state are shared inside a sandbox, so reusing one across
    unrelated work leaks state between them.

    `backend` defaults to "auto", which picks whichever container engine
    is actually running and reports which one it was. First use of a
    language may take a minute while its image is pulled; progress is
    reported while that happens.

    Pass `environment` to start from a heavier prebuilt image — for
    example one that already has numpy and pandas, so you do not pay a
    package install on every run. The `hyperbox://capabilities` resource
    lists the ones that exist. You cannot create an environment: only the
    user can, with `hyperbox build` at their terminal.

    Read the `hyperbox://capabilities` resource for exact limits.
    """
    try:
        language = validate.language(language)
        requested = validate.backend(backend)
        environment = validate.environment(environment)
    except InvalidInput as exc:
        return {"error": str(exc)}

    try:
        resolved = await asyncio.to_thread(engine.detect, requested)
    except EngineUnavailableError as exc:
        return {"error": str(exc)}
    except UnsupportedBackendError as exc:
        return {"error": str(exc)}

    # Say what the slow part is going to be, so a first run reads as a
    # download rather than a hang.
    try:
        wanted_image = (
            policy.environments()[environment]
            if environment
            else image_for(language)
        )
        cold = not await asyncio.to_thread(
            engine.image_present, resolved, wanted_image
        )
    except Exception:  # noqa: BLE001 - only used to word the message
        cold = False
    what = (
        f"pulling the {language} image (first use, several GB)"
        if cold
        else f"starting {language} sandbox"
    )

    # Claim the id BEFORE the container exists. Garbage collection in any
    # process skips reservations, so nothing can reclaim the container
    # that is about to be created under this id.
    sandbox_id = uuid.uuid4().hex[:12]
    _registry.reserve(sandbox_id, language=language, backend=resolved)
    try:
        async with _heartbeat(ctx, what):
            # Offloaded: container creation blocks for seconds to minutes,
            # and must not stall the server's event loop.
            handle = await asyncio.to_thread(
                _runtime.create,
                language=language,
                backend=resolved,
                sandbox_id=sandbox_id,
                environment=environment,
            )
    except (
        UnsupportedLanguageError,
        UnsupportedEnvironmentError,
        UnsupportedBackendError,
        InvalidInput,
    ) as exc:
        _registry.remove(sandbox_id)
        return {"error": str(exc)}
    except EngineUnavailableError as exc:
        _registry.remove(sandbox_id)
        return {"error": str(exc)}
    except SandboxRuntimeError as exc:
        _registry.remove(sandbox_id)
        return {"error": f"Failed to create sandbox: {exc}"}
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        _registry.remove(sandbox_id)
        return {
            "error": f"Failed to create sandbox: {type(exc).__name__}: {exc}"
        }

    _registry.finalize(sandbox_id, handle.meta.get("container_ref", ""))
    await ctx.report_progress(100, 100, "sandbox ready")
    return {
        "sandbox_id": handle.sandbox_id,
        "language": handle.language,
        "backend": handle.backend,
        "environment": environment,
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
    timeout: float | None = policy.DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Execute code in a sandbox and return exactly what happened.

    Returns {stdout, stderr, exit_code, success} — never a bare "it
    failed", so you can read the real traceback and fix the actual cause.
    Prefer this over running generated code on the user's machine.

    Safe to call repeatedly on one sandbox_id, and that is the intended
    shape: one sandbox per task, many runs. The filesystem and installed
    packages persist between calls; in-memory variables do NOT — each run
    is a fresh process, so write anything you need to keep to a file.
    Write working files to /work: it is scratch space, size-limited, and
    discarded with the sandbox.

    Your code runs with NO network access. Passing `libraries` installs
    them in a brief, separate network-enabled step first, then re-seals
    before your code executes; only plain package names are accepted.
    `timeout` is capped by the server, must be a positive number, and may
    not be null. Output is truncated past a limit, and marked when it is.
    """
    try:
        sandbox_id = validate.sandbox_id(sandbox_id)
        code = validate.code(code)
        libraries = validate.libraries(libraries)
        timeout = validate.timeout(timeout)
    except InvalidInput as exc:
        return {"error": str(exc)}

    try:
        # Held for the whole call so two processes cannot drive the same
        # container's session concurrently, and re-read inside the lock so
        # a destroy that landed first is seen.
        with _registry.lock(sandbox_id):
            rec = _registry.get_ready(sandbox_id)
            if rec is None:
                return _no_sandbox(sandbox_id)
            result = _runtime.run(
                _handle_for(rec), code=code, libraries=libraries, timeout=timeout
            )
            _registry.touch(sandbox_id)
    except EngineUnavailableError as exc:
        return {"error": str(exc)}
    except SandboxRuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # Results are ALWAYS structured. An unexpected backend exception
        # must reach the agent as something it can reason about, not as a
        # crashed tool call.
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "stdout": _cap_output(result.stdout),
        "stderr": _cap_output(result.stderr),
        "exit_code": result.exit_code,
        "success": result.success,
        "timed_out": result.timed_out,
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

    If the container engine cannot be reached this returns an error and
    keeps the sandbox on file, rather than claiming a cleanup it could not
    verify.
    """
    try:
        sandbox_id = validate.sandbox_id(sandbox_id)
    except InvalidInput as exc:
        return {"error": str(exc)}

    try:
        with _registry.lock(sandbox_id):
            rec = _registry.get(sandbox_id)
            if rec is None:
                # Idempotent, so this is a success. It reports an
                # affirmative status rather than a boolean: `destroyed:
                # false` on a call that worked invites an agent to retry
                # or error-handle it.
                return {"sandbox_id": sandbox_id, "status": "already_gone"}
            handle = _handle_for(rec)
            # Raises if the engine cannot be reached, which leaves the
            # record in place — see the runtime's destroy() contract.
            _runtime.destroy(handle)
            if _runtime.alive(handle):
                return {
                    "error": (
                        f"Sandbox '{sandbox_id}' container is still running "
                        "after destroy was attempted. The sandbox is still "
                        "on file; try again."
                    )
                }
            _registry.remove(sandbox_id)
    except EngineUnavailableError as exc:
        return {
            "error": (
                f"{exc} The sandbox is still on file and was NOT removed, "
                "because a container that cannot be reached has not been "
                "proven gone."
            )
        }
    except SandboxRuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        return {"error": f"{type(exc).__name__}: {exc}"}
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


def serve() -> None:
    # Reclaim anything left behind by a previous process before serving.
    collect_garbage()
    mcp.run()


def main() -> None:
    """Entry point for the `hyperbox` command.

    With no arguments it starts the stdio MCP server, so an existing
    client configuration keeps working unchanged. Subcommands are for
    humans at a terminal and never write to the stdio channel a client
    is using.
    """
    argv = sys.argv[1:]
    if argv and argv[0] in {
        "doctor", "config", "envs", "build",
        "--version", "-V", "help", "--help", "-h",
    }:
        from hyperbox_mcp.cli import dispatch

        raise SystemExit(dispatch(argv))
    if argv:
        from hyperbox_mcp.cli import usage

        print(f"hyperbox: unknown argument {argv[0]!r}\n", file=sys.stderr)
        print(usage(), file=sys.stderr)
        raise SystemExit(2)
    serve()


if __name__ == "__main__":
    main()
