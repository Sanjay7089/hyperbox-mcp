"""HyperBox: a container an agent can run code in without risking your machine.

Run with: uv run python -m hyperbox_mcp.server

The tools below talk ONLY to a Runtime (see runtime.py). They must never
import a backend directly — that is what keeps the execution backend
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
import inspect
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress

# Set BEFORE fastmcp is imported: its Settings read the environment once,
# at import, and both of these default to on.
#
# The update check is an outbound HTTPS call to PyPI on every server
# start. A client launches a fresh server per window and relaunches it
# freely, so this log has 835 starts in it -- 835 calls home from a
# process whose stated design is that nothing leaves the machine (see
# CLAUDE.md, and the no-egress rule it turns on). It also costs real
# latency: initialize measured 0.84s mean with it on, 0.19s with it off,
# and the spread came entirely from the network. On a machine with slow
# DNS that is startup time a client can time out waiting for.
#
# setdefault, not assignment: someone who genuinely wants either can
# still set it in the client config.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
# Pure noise on a stdio server -- it renders an ANSI box on stderr that
# no one reads, at every launch.
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")

from fastmcp import Context, FastMCP  # noqa: E402 - must follow the env defaults

from hyperbox_mcp import engine, errors, policy, slots, validate
from hyperbox_mcp.engine import EngineUnavailableError
from hyperbox_mcp.errors import (
    UnknownEnvironmentError as UnsupportedEnvironmentError,
)
from hyperbox_mcp.errors import (
    UnsupportedBackendError,
    UnsupportedLanguageError,
)
from hyperbox_mcp.sandbox_ops import SandboxRuntimeError
from hyperbox_mcp.registry import Registry
from hyperbox_mcp.runtime import Runtime, SandboxHandle
from hyperbox_mcp.validate import InvalidInput

#: Re-exported from policy so `from hyperbox_mcp.server import LOG_FILE`
#: keeps working; policy is where they live now, because the CLI needs
#: them without paying for this module.
LOG_DIR = policy.LOG_DIR
LOG_FILE = policy.LOG_FILE
LOG_MAX_BYTES = policy.LOG_MAX_BYTES
LOG_BACKUPS = policy.LOG_BACKUPS

logger = logging.getLogger("hyperbox")


def _setup_logging() -> None:
    """Attach the rotating file handler. Called from serve(), NOT at import.

    A stdio server cannot print: stdout is the JSON-RPC channel and the
    client is reading it. A file is the only place a record of what
    happened can go.

    This is not done at import because the console script is
    `hyperbox_mcp.server:main`, so every CLI invocation — `hyperbox
    doctor`, `hyperbox config` — imports this module. Configuring at
    import would create and open a log file as a side effect of asking
    for the version.

    It rotates: an unbounded log on a server that runs for weeks is a
    disk leak, not a diagnostic.
    """
    if logger.handlers:  # serve() called twice in one process
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError as exc:
        # An unwritable log directory must not stop the server from
        # serving. Say so on stderr, which is safe, and carry on.
        print(f"hyperbox: logging disabled ({exc})", file=sys.stderr)
        return
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Never propagate to the root logger: a root StreamHandler would put
    # log lines on stdout, in the middle of the JSON-RPC stream.
    logger.propagate = False


mcp = FastMCP("HyperBox")

#: Which execution backend to use.
#:
#: `native` speaks the engine's REST API directly and takes no execution
#: dependency at all. It is the default because it does more and does it
#: better: five languages against one, packages installed before the
#: sandbox is sealed rather than through a window re-opened per run, a
#: timeout that kills a forked child, and images that are official and
#: tagged rather than mutable `latest` from one personal namespace.
#:
#: llm-sandbox was removed in 0.4.0. HYPERBOX_RUNTIME is still read so a
#: config that names it is REFUSED with an explanation rather than
#: silently ignored -- a setting that no longer does what it says should
#: say so, not shrug.
RUNTIME_CHOICES = ("native",)


def select_runtime(choice: str | None = None) -> Runtime:
    """The execution backend.

    One implementation since 0.4. `choice` is kept so callers and tests
    read unchanged, and any value other than "native" is refused rather
    than silently ignored -- a configuration that no longer does what it
    says should say so.
    """
    name = (choice or os.environ.get("HYPERBOX_RUNTIME") or "native").strip().lower()
    if name != "native":
        raise ValueError(
            f"HYPERBOX_RUNTIME={name!r} is no longer supported. The "
            "llm-sandbox backend was removed in 0.4.0; unset the variable "
            "to use the native runtime."
        )
    from hyperbox_mcp.native_runtime import NativeRuntime

    return NativeRuntime()


def _create_parameters() -> frozenset[str]:
    """Which optional arguments the active runtime's create() accepts.

    Read from the signature rather than tracked by hand: the two runtimes
    genuinely differ, and a list maintained separately drifts silently
    into passing a keyword that raises TypeError.
    """
    global _create_params
    if _create_params is None:
        _create_params = frozenset(
            inspect.signature(_runtime.create).parameters
        )
    return _create_params


_create_params: frozenset[str] | None = None


# The one place a concrete backend is chosen.
_runtime: Runtime = select_runtime()

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
    return errors.to_result(
        errors.SandboxStaleError(
            f"No sandbox '{sandbox_id}'. It was never created, was already "
            "destroyed, or was reclaimed after its inactivity timeout.",
            fix="Call create_sandbox and use the sandbox_id it returns.",
            context={"sandbox_id": sandbox_id},
        )
    )


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
                    logger.info("GC: reclaiming expired sandbox %s", rec.sandbox_id)
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
        logger.exception("engine-level GC failed")
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
            # What the running runtime can deliver, not a constant: the
            # answer differs between backends and this resource is what an
            # agent plans against.
            "languages": sorted(_runtime.supported_languages()),
            # Named, because it changes what a sandbox can deliver and was
            # otherwise inferable only by counting the languages above.
            "runtime": type(_runtime).__name__,
            # Resolved per request, so an environment the user built
            # after this server started is listed without a restart.
            #
            # Objects rather than names: an agent choosing between
            # "data-science" and "live-test" from names alone is guessing,
            # and the image is what tells it which one has what it needs.
            "environments": [
                {
                    "name": name,
                    "image": image,
                    # Always "sealed" since 0.4.0, and stated rather than
                    # omitted: an agent reading this should not have to
                    # infer a security property from a missing key. An
                    # environment used to be able to keep its network,
                    # which is what made this field a discriminator.
                    "network": "sealed",
                }
                for name, image in sorted(policy.environments().items())
            ],
            # What this server CANNOT do, and the command a human runs to
            # do it. Building an environment is deliberately not a tool
            # (see create_sandbox), so without this an agent discovers the
            # boundary by failing -- which is the thing this resource
            # exists to prevent.
            "host_actions": {
                "note": (
                    "These run on the user's machine, not in a sandbox. "
                    "You cannot run them. Ask the user to, then retry."
                ),
                "create_environment": (
                    "hyperbox build <name> --image <ref>   "
                    "(or --dockerfile <path> to build one)"
                ),
                "list_environments": "hyperbox envs",
                "check_setup": "hyperbox doctor",
                "start_an_engine": (
                    "open Docker Desktop, or `podman machine start`"
                ),
            },
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
                "inbound_from_the_host": (
                    "impossible. No port is published to the user's machine, "
                    "so a server you start in a sandbox is reachable only "
                    "from inside that same sandbox, on 127.0.0.1. Never "
                    "report a URL to the user as something they can open."
                ),
                "when_it_is_ever_enabled": (
                    "only while the sandbox is being created, to install the "
                    "packages you declared, and before any of your code has "
                    "run. It is then detached and verified unreachable — a "
                    "TCP connection and a DNS lookup must both fail — and "
                    "never re-attached."
                ),
                "declare_packages_with": "create_sandbox(packages=[...])",
                "installable": (
                    "named packages from the default index only; flags, "
                    "URLs, paths and VCS references are refused"
                ),
                "deprecated": (
                    "run(libraries=[...]) still works but re-opens the "
                    "network mid-session; it will be refused in a future "
                    "release"
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
    packages: list[str] | None = None,
    sync_from: str | None = None,
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

    DECLARE EVERY PACKAGE YOU NEED IN `packages`. They are installed while
    the sandbox is being built, and the network is then cut off for good —
    so a package you did not ask for here cannot be installed later. This
    is the only moment a sandbox can reach the internet, and it happens
    before any of your code runs. Ask for what you need up front rather
    than discovering it and retrying; a sandbox that turns out to be
    missing something is cheaper to replace than to patch.

    Not every language takes packages: `java` refuses them and says what to
    do instead. `hyperbox://capabilities` lists which do.

    WHICH TO USE. A handful of pure-Python packages: `packages`. A heavy
    or native stack — torch, pandas, a JDK toolchain, anything that
    compiles — or one you will want again on the next task: ask the user
    for an environment instead. Installing those on every create costs
    minutes each time, and the install happens inside the one window where
    the sandbox has network. A long install can also outlive your client's
    own tool-call timeout, so the call fails even though the sandbox was
    fine.

    The command to give the user, verbatim:

        hyperbox build <name> --dockerfile <path>    (or --image <ref>)

    then `create_sandbox(environment="<name>")`. It is repeated here
    rather than only in `hyperbox://capabilities` because some clients
    never read resources, and an escape hatch nobody can see is not one.

    `sync_from` copies a directory from the user's machine into the
    sandbox at /sandbox, so you can run their real code and their real
    test data instead of a snippet you retyped. It is OFF unless the user
    has allowed it: they run `hyperbox init` once, in a directory they are
    willing to share, and only paths under an allowed directory are
    accepted. You cannot run `hyperbox init` for them — ask.

    The result carries a `sync` manifest saying how many files arrived and
    naming every one that did not, with a reason: secrets like .env are
    never copied, and a .hyperboxignore is honoured. READ IT. If a file
    you expected is missing it will be named there, which is faster and
    more honest than guessing why an import failed.

    Read the `hyperbox://capabilities` resource for exact limits.
    """
    try:
        language = validate.language(language, _runtime.supported_languages())
        synced_from = validate.sync_from(sync_from)
        requested = validate.backend(backend)
        environment = validate.environment(environment)
        packages = validate.libraries(packages)
    except (InvalidInput, UnsupportedLanguageError) as exc:
        return errors.to_result(exc)

    try:
        resolved = await asyncio.to_thread(engine.detect, requested)
    except (EngineUnavailableError, UnsupportedBackendError) as exc:
        return errors.to_result(exc)

    # Say what the slow part is going to be, so a first run reads as a
    # download rather than a hang.
    try:
        wanted_image = (
            _runtime.image_for(language, environment)
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
    logger.info(
        "creating sandbox %s (language=%s, backend=%s, environment=%s)",
        sandbox_id, language, resolved, environment,
    )
    # The short reservation TTL, not the default: until this row is
    # finalised it shields a container that may be running, root and still
    # networked. See policy.CREATING_TTL_SECONDS.
    _registry.reserve(sandbox_id, language=language, backend=resolved,
                      ttl_seconds=policy.CREATING_TTL_SECONDS)
    try:
        async with _heartbeat(ctx, what):
            # Offloaded: container creation blocks for seconds to minutes,
            # and must not stall the server's event loop.
            create_kwargs = dict(
                language=language,
                backend=resolved,
                sandbox_id=sandbox_id,
                environment=environment,
            )
            # Only the native runtime provisions at create time or syncs
            # files in. Passing either to a runtime that cannot honour it
            # would silently drop it -- and passing a keyword its create()
            # does not take raises a TypeError the caller cannot act on.
            # So the capability is checked, and an unsupported request is
            # REFUSED with a reason rather than ignored.
            accepted = _create_parameters()
            for name, value in (("packages", packages),
                                ("sync_from", synced_from)):
                if not value:
                    continue
                if name not in accepted:
                    _registry.remove(sandbox_id)
                    return errors.to_result(InvalidInput(
                        f"The active runtime ({type(_runtime).__name__}) "
                        f"cannot honour `{name}`.",
                        fix="Unset HYPERBOX_RUNTIME to use the default "
                            "native runtime, which supports it.",
                        context={"runtime": type(_runtime).__name__,
                                 "parameter": name},
                    ))
                create_kwargs[name] = value
            def create_under_slot():
                # Bounded across processes: several servers commonly share
                # one engine, and simultaneous image pulls are what
                # saturates it.
                with slots.engine_slot(f"creating {language} sandbox"):
                    return _runtime.create(**create_kwargs)

            handle = await asyncio.to_thread(create_under_slot)
    except (
        UnsupportedLanguageError,
        UnsupportedEnvironmentError,
        UnsupportedBackendError,
        InvalidInput,
    ) as exc:
        _registry.remove(sandbox_id)
        return errors.to_result(exc)
    except (EngineUnavailableError, SandboxRuntimeError) as exc:
        _registry.remove(sandbox_id)
        return errors.to_result(exc)
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        _registry.remove(sandbox_id)
        return errors.to_result(exc)

    _registry.finalize(sandbox_id, handle.meta.get("container_ref", ""))
    logger.info("sandbox %s ready", sandbox_id)
    await ctx.report_progress(100, 100, "sandbox ready")
    return {
        "sandbox_id": handle.sandbox_id,
        "language": handle.language,
        "backend": handle.backend,
        "environment": environment,
        "packages": packages or [],
        # Unconditional: create() seals every sandbox and proves it from
        # inside, or destroys the container. Reaching this line at all
        # means the probe came back blocked, so this is a read-back and
        # not a promise.
        "network": "sealed — this sandbox cannot reach the internet",
        **({"sync": handle.meta["sync"]} if handle.meta.get("sync") else {}),
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
async def run(
    ctx: Context,
    sandbox_id: str,
    code: str,
    libraries: list[str] | None = None,
    timeout: float | None = policy.DEFAULT_TIMEOUT_SECONDS,
    background: bool = False,
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

    Your code runs with NO network access.

    `libraries` is DEPRECATED: declare what you need in
    `create_sandbox(packages=[...])` instead, which installs before the
    sandbox is sealed and keeps it sealed for its whole life. Passing it
    here still works for now — it briefly re-opens the network, installs,
    and re-seals — but that window is exactly what create-time
    provisioning removes, and it will be refused in a future release.
    `timeout` is capped by the server, must be a positive number, and may
    not be null. Output is truncated past a limit, and marked when it is.

    `background=True` starts the code and returns immediately with a
    `process_id` instead of output. Use it for something that is meant to
    keep running — a web server, a worker — and then `run` a normal
    foreground call in the SAME sandbox to talk to it on 127.0.0.1.
    Loopback works even though the sandbox has no route to the internet.

    THAT PORT IS REACHABLE ONLY FROM INSIDE THIS SANDBOX. Nothing is
    published to the user's machine: they cannot open it in a browser,
    curl it from their terminal, or point another program at it. Do not
    hand them a URL — it will not resolve, and saying "your app is running
    at http://localhost:8000" is a claim this server cannot honour. To
    show that a service works, `run` a foreground call that requests it on
    127.0.0.1 and report what came back.

    Read its output with `get_process_logs`. `timeout` does not apply to
    a background run: nothing stops it but `destroy_sandbox`, or the
    sandbox expiring — and reading its logs counts as use, which holds
    that off. Its log is capped, and a process that writes past the cap
    is stopped there rather than allowed to fill the disk.

    On timeout the code is actually killed, not just abandoned — a timed-out
    run leaves nothing burning CPU in the sandbox, and the sandbox stays
    usable. If the code cannot be killed the sandbox is restarted instead,
    which empties scratch space; the result says which happened.
    """
    try:
        sandbox_id = validate.sandbox_id(sandbox_id)
        code = validate.code(code)
        libraries = validate.libraries(libraries)
        timeout = validate.timeout(timeout)
    except InvalidInput as exc:
        return errors.to_result(exc)

    if background and not hasattr(_runtime, "start_background"):
        return errors.to_result(InvalidInput(
            f"The active runtime ({type(_runtime).__name__}) cannot run "
            "code in the background.",
            fix="Unset HYPERBOX_RUNTIME to use the default native runtime.",
        ))

    def launch():
        """Start it and let go. Returns the id its logs are filed under."""
        with _registry.lock(sandbox_id):
            rec = _registry.get_ready(sandbox_id)
            if rec is None:
                return None
            process_id = _runtime.start_background(_handle_for(rec), code)
            _registry.touch(sandbox_id)
            return process_id

    def execute():
        # The lock is held for the whole call so two processes cannot drive
        # one container concurrently, and the record is re-read inside it
        # so a destroy that landed first is seen.
        with _registry.lock(sandbox_id):
            rec = _registry.get_ready(sandbox_id)
            if rec is None:
                return None
            outcome = _runtime.run(
                _handle_for(rec), code=code, libraries=libraries, timeout=timeout
            )
            _registry.touch(sandbox_id)
            return outcome

    if background:
        try:
            process_id = await asyncio.to_thread(launch)
        except (EngineUnavailableError, SandboxRuntimeError) as exc:
            return errors.to_result(exc)
        except Exception as exc:  # noqa: BLE001 - always structured
            return errors.to_result(exc)
        if process_id is None:
            return errors.to_result(errors.SandboxStaleError(
                f"No sandbox '{sandbox_id}' is ready.",
                fix="Call create_sandbox first.",
                context={"sandbox_id": sandbox_id},
            ))
        logger.info("background run %s started in %s", process_id, sandbox_id)
        return {
            "sandbox_id": sandbox_id,
            "process_id": process_id,
            "status": "running",
            "logs_with": (
                f"get_process_logs(sandbox_id='{sandbox_id}', "
                f"process_id='{process_id}')"
            ),
            "note": (
                "Started and left running. `timeout` does not apply to a "
                "background run and was ignored. Nothing stops it except "
                "destroy_sandbox, or the sandbox expiring — reading its "
                "logs counts as use and holds that off."
            ),
        }

    try:
        # Offloaded and reported on. A run can legitimately take the full
        # timeout, and before this the tool went silent for all of it --
        # silence a client resolves by killing the request, which loses the
        # result of work that had already finished.
        async with _heartbeat(ctx, "running your code"):
            result = await asyncio.to_thread(execute)
        if result is None:
            return _no_sandbox(sandbox_id)
        logger.info(
            "run in %s finished (exit_code=%s, timed_out=%s)",
            sandbox_id, result.exit_code, result.timed_out,
        )
    except (EngineUnavailableError, SandboxRuntimeError) as exc:
        return errors.to_result(exc)
    except Exception as exc:  # noqa: BLE001
        # Results are ALWAYS structured. An unexpected backend exception
        # must reach the agent as something it can reason about, not as a
        # crashed tool call.
        return errors.to_result(exc)
    payload = {
        "stdout": _cap_output(result.stdout),
        "stderr": _cap_output(result.stderr),
        "exit_code": result.exit_code,
        "success": result.success,
        "timed_out": result.timed_out,
    }
    if libraries:
        payload["deprecation"] = (
            "run(libraries=...) is deprecated and will be refused in a "
            "future release: it re-opens the network mid-session. Declare "
            "packages in create_sandbox(packages=[...]) instead, which "
            "installs before the sandbox is sealed."
        )
    return payload


@mcp.tool(
    annotations={
        "title": "Read a background run's output",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_process_logs(
    ctx: Context, sandbox_id: str, process_id: str
) -> dict:
    """Read what a background run has printed so far.

    Use this after `run(background=True)`. Both stdout and stderr, merged,
    from the start of the process — a background run has no single moment
    to hand output back, so it accumulates in a file you read whenever you
    like.

    Reading counts as using the sandbox, so polling a long-running server
    keeps it from expiring. A sandbox nobody touches is reclaimed after
    its inactivity timeout, and a daemon nothing reads does not count as
    activity by itself.

    The log is capped. A process that writes more than the cap is stopped
    at that point rather than being allowed to fill the disk.

    Nothing here stops a background process. `destroy_sandbox` is how one
    ends; there is no separate stop.
    """
    try:
        sandbox_id = validate.sandbox_id(sandbox_id)
        process_id = validate.process_id(process_id)
    except InvalidInput as exc:
        return errors.to_result(exc)

    if not hasattr(_runtime, "background_logs"):
        return errors.to_result(InvalidInput(
            f"The active runtime ({type(_runtime).__name__}) has no "
            "background runs.",
            fix="Unset HYPERBOX_RUNTIME to use the default native runtime.",
        ))

    def read():
        with _registry.lock(sandbox_id):
            rec = _registry.get_ready(sandbox_id)
            if rec is None:
                return None
            out = _runtime.background_logs(_handle_for(rec), process_id)
            # Reading is using. Without this a polled daemon is reclaimed
            # out from under the caller that is watching it.
            _registry.touch(sandbox_id)
            return out

    try:
        out = await asyncio.to_thread(read)
    except (EngineUnavailableError, SandboxRuntimeError) as exc:
        return errors.to_result(exc)
    except Exception as exc:  # noqa: BLE001 - always structured
        return errors.to_result(exc)

    if out is None:
        return errors.to_result(errors.SandboxStaleError(
            f"No sandbox '{sandbox_id}' is ready.",
            fix="Call create_sandbox first.",
            context={"sandbox_id": sandbox_id},
        ))
    return {
        "sandbox_id": sandbox_id,
        "process_id": process_id,
        "output": _cap_output(out),
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
async def destroy_sandbox(ctx: Context, sandbox_id: str) -> dict:
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
        return errors.to_result(exc)

    def teardown() -> dict | None:
        """Returns a result to send back, or None when it plainly worked."""
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
                return errors.to_result(
                    SandboxRuntimeError(
                        f"Sandbox '{sandbox_id}' container is still running "
                        "after destroy was attempted. The sandbox is still "
                        "on file.",
                        fix="Call destroy_sandbox again.",
                        context={"sandbox_id": sandbox_id},
                    )
                )
            _registry.remove(sandbox_id)
            logger.info("destroyed sandbox %s", sandbox_id)
            return None

    try:
        # Reported on, because this waits for another process's lock when
        # two clients touch one sandbox, and a silent wait is what a client
        # kills.
        async with _heartbeat(ctx, "destroying the sandbox"):
            early = await asyncio.to_thread(teardown)
        if early is not None:
            return early
    except EngineUnavailableError as exc:
        # The record is deliberately KEPT: a container that cannot be
        # reached has not been proven gone, and forgetting it here is how
        # orphans accumulate.
        exc.context.setdefault("sandbox_id", sandbox_id)
        exc.context["registry_row"] = "kept — removal could not be verified"
        return errors.to_result(exc)
    except SandboxRuntimeError as exc:
        return errors.to_result(exc)
    except Exception as exc:  # noqa: BLE001 — see run() for the rationale
        return errors.to_result(exc)
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


def _background_gc_loop() -> None:
    """Sweep for expired sandboxes for as long as the server is up.

    Sweeps immediately, then on the interval. The first sweep used to run
    synchronously in serve(), before mcp.run() — so the server did not
    start listening until every engine had been probed, and probing a
    stopped Podman costs a CLI subprocess with a multi-second timeout.
    Several clients launching at once then all looked like servers that had
    failed to start. Nothing about reclaiming an old container needs to
    happen before the first tool call can be answered.

    Without the loop, the inactivity TTL would be enforced only by
    restarting the process, and a server running inside an editor stays up
    for days.

    No new race: collect_garbage() takes the per-sandbox registry lock, and
    so does every tool that touches one.
    """
    while True:
        try:
            reclaimed = collect_garbage()
        except Exception:  # noqa: BLE001 - a failed sweep is not fatal
            # Reported, not swallowed. A GC that quietly stops working
            # looks exactly like a GC with nothing to do.
            logger.exception("background GC sweep failed")
        else:
            if reclaimed:
                logger.info(
                    "background GC reclaimed %d container(s)", len(reclaimed)
                )
        time.sleep(policy.GC_INTERVAL_SECONDS)


def serve() -> None:
    # Before the sweep, so the first collection is on the record.
    _setup_logging()
    # Name the runtime. It is chosen once at import from HYPERBOX_RUNTIME,
    # and the client launches this process with its own environment -- so
    # `hyperbox doctor` in a terminal reports what IT would use, which is
    # not necessarily what the running server used. Until this line, the
    # only way to tell was to count the languages in capabilities.
    from hyperbox_mcp.cli import _version

    logger.info(
        "HyperBox server starting (runtime: %s, version: %s)",
        type(_runtime).__name__, _version(),
    )

    # Reclaiming what a previous process left behind happens on the GC
    # thread's first pass, NOT here. Doing it here delayed the server's
    # first response by however long it took to probe both engines, which
    # on a machine with a stopped Podman is a CLI subprocess with a
    # multi-second timeout. Daemon, so it never holds the process open at
    # shutdown.
    threading.Thread(
        target=_background_gc_loop, daemon=True, name="hyperbox-gc"
    ).start()

    mcp.run()


def main() -> None:
    """Kept so `python -m hyperbox_mcp.server` still works.

    The `hyperbox` entry point is cli.main, which dispatches subcommands
    without importing this module at all. Anyone already here has paid
    for the import, so this just forwards.
    """
    from hyperbox_mcp.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
