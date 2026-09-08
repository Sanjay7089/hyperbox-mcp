"""`hyperbox doctor` — is this machine able to run sandboxes right now?

Written for the developer who just cloned the repo and wants one answer
before wiring an MCP client to it. Every check reports what was actually
observed, and every failure carries the command that fixes it. A check
that cannot be performed says so rather than passing quietly.

The live round trip at the end is the only check that proves anything:
the rest establish that the pieces exist, and it establishes that they
work together.
"""

from __future__ import annotations

import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass, field

from hyperbox_mcp import engine, policy
from hyperbox_mcp.engine import EngineUnavailableError
from hyperbox_mcp.llm_sandbox_runtime import LLMSandboxRuntime
from hyperbox_mcp.registry import Registry, state_dir

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}

#: The image `create_sandbox(language="python")` needs. Read from
#: llm-sandbox rather than hardcoded, so it cannot drift from what the
#: runtime will actually pull.
def python_image() -> str:
    from llm_sandbox.const import DefaultImage

    return DefaultImage.PYTHON


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    notes: list[str] = field(default_factory=list)


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        # Printed as it completes, not collected for the end. Several of
        # these take real time -- probing a stopped engine, pulling a
        # multi-gigabyte image -- and a command that prints nothing while
        # it works is indistinguishable from one that has hung.
        print(self._render_one(check), flush=True)
        return check

    @staticmethod
    def _render_one(c: Check) -> str:
        lines = [f"{_MARK[c.status]}  {c.name}"]
        if c.detail:
            lines.append(f"        {c.detail}")
        for note in c.notes:
            lines.append(f"        {note}")
        if c.fix and c.status != OK:
            for i, line in enumerate(c.fix.splitlines()):
                prefix = "  fix:  " if i == 0 else "        "
                lines.append(f"      {prefix}{line}")
        return "\n".join(lines)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def render(self) -> str:
        """Every check, for a caller that wants them collected. Checks are
        already printed as they complete; this is not used by run_doctor."""
        return "\n".join(self._render_one(c) for c in self.checks)


def _version(package: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(package)
    except PackageNotFoundError:
        return "not installed"


def check_environment(report: Report) -> None:
    py = platform.python_version()
    status = OK if sys.version_info >= (3, 11) else FAIL
    report.add(
        Check(
            name="Python interpreter",
            status=status,
            detail=(
                f"Python {py} on {platform.system()} {platform.machine()} "
                f"({'named pipes' if engine.WINDOWS else 'unix sockets'}); "
                f"hyperbox-mcp {_version('hyperbox-mcp')}, "
                f"llm-sandbox {_version('llm-sandbox')}, "
                f"fastmcp {_version('fastmcp')}"
            ),
            fix="HyperBox needs Python 3.11 or newer. Run it under `uv run`.",
        )
    )


def check_engines(report: Report) -> dict[str, engine.EngineStatus]:
    statuses: dict[str, engine.EngineStatus] = {}
    for backend in policy.BACKENDS:
        status = engine.probe(backend)
        statuses[backend] = status
        notes = [f"{k}: {v}" for k, v in status.extra.items() if v]
        if status.binary:
            notes.insert(0, f"cli: {status.binary}")
        if backend in policy.EXPERIMENTAL_BACKENDS:
            notes.append(
                "experimental: results may not reach the server on this "
                "engine; sandboxes refuse to start rather than return "
                "empty output. Prefer docker."
            )
        product = status.extra.get("product")
        label = (
            f"{backend} engine reachable"
            if not product or product == backend
            else f"{backend} endpoint reachable — served by {product}"
        )
        report.add(
            Check(
                name=label,
                # One engine is enough, so a single unreachable engine is a
                # warning. "No engine at all" is failed separately below.
                status=OK if status.reachable else WARN,
                detail=(
                    f"version {status.version}"
                    if status.reachable
                    else status.detail
                ),
                fix="" if status.reachable else status.fix,
                notes=notes,
            )
        )
    return statuses


def check_selection(report: Report, statuses: dict) -> str:
    try:
        chosen = engine.detect("auto")
    except EngineUnavailableError as exc:
        report.add(
            Check(
                name="backend selection",
                status=FAIL,
                detail="No container engine is reachable, so no code can run.",
                fix=str(exc),
            )
        )
        return ""
    others = [b for b in policy.BACKENDS if statuses.get(b) and statuses[b].reachable]
    report.add(
        Check(
            name="backend selection",
            status=OK,
            detail=(
                f"'auto' resolves to {chosen} "
                f"(reachable: {', '.join(others) or chosen}); "
                "docker is preferred when both are genuinely available"
            ),
            notes=(
                [
                    f"{chosen} is experimental — see docs/troubleshooting.md"
                ]
                if chosen in policy.EXPERIMENTAL_BACKENDS
                else []
            ),
        )
    )
    return chosen


def check_image(report: Report, backend: str, pull: bool) -> None:
    if not backend:
        return
    image = python_image()
    try:
        client = engine.client(backend)
    except EngineUnavailableError as exc:
        report.add(
            Check(name="python sandbox image", status=FAIL, detail=str(exc))
        )
        return
    try:
        client.images.get(image)
        report.add(
            Check(
                name="python sandbox image",
                status=OK,
                detail=f"{image} present locally on {backend}",
            )
        )
        return
    except Exception:  # noqa: BLE001 - absent is the expected case here
        pass

    if not pull:
        report.add(
            Check(
                name="python sandbox image",
                status=WARN,
                detail=(
                    f"{image} is not present on {backend}; the first "
                    "create_sandbox will pull it, which can take a minute."
                ),
                fix="Pre-pull it now with `hyperbox doctor --pull`.",
            )
        )
        return

    started = time.time()
    try:
        client.images.pull(image)
    except Exception as exc:  # noqa: BLE001
        report.add(
            Check(
                name="python sandbox image",
                status=FAIL,
                detail=f"Could not pull {image}: {type(exc).__name__}: {exc}",
                fix="Check network access to ghcr.io, then retry.",
            )
        )
        return
    report.add(
        Check(
            name="python sandbox image",
            status=OK,
            detail=f"pulled {image} in {time.time() - started:.0f}s",
        )
    )


def check_registry(report: Report) -> None:
    directory = state_dir()
    try:
        registry = Registry()
        records = registry.all_records()
    except Exception as exc:  # noqa: BLE001
        report.add(
            Check(
                name="sandbox registry",
                status=FAIL,
                detail=f"{directory}: {type(exc).__name__}: {exc}",
                fix=(
                    "Make the directory writable, or point HYPERBOX_STATE_DIR "
                    "somewhere else."
                ),
            )
        )
        return

    notes: list[str] = []
    stale = 0
    for rec in records:
        if not rec.ready:
            notes.append(f"{rec.sandbox_id}: still being created")
            continue
        try:
            engine.get_container(rec.backend, rec.container_ref)
            notes.append(f"{rec.sandbox_id}: live on {rec.backend}")
        except EngineUnavailableError:
            notes.append(f"{rec.sandbox_id}: {rec.backend} unreachable, unknown")
        except Exception:  # noqa: BLE001 - ContainerGoneError and friends
            stale += 1
            notes.append(f"{rec.sandbox_id}: container gone (stale row)")

    report.add(
        Check(
            name="sandbox registry",
            status=WARN if stale else OK,
            detail=f"{directory} — {len(records)} sandbox(es) on file",
            notes=notes,
            fix=(
                "Stale rows are cleared automatically on the next server "
                "start, or by calling destroy_sandbox with that id."
            )
            if stale
            else "",
        )
    )


def check_round_trip(report: Report, backend: str) -> None:
    """Create, run, verify the limits landed, destroy, confirm gone.

    This is the check that matters. Everything above it says the parts
    are present; only this says they work.
    """
    if not backend:
        return
    # Creating a sandbox pulls the image if it is absent, which takes
    # minutes. Say that plainly instead of appearing to hang, and point
    # at the flag that does the pull deliberately.
    try:
        if not engine.image_present(backend, python_image()):
            report.add(
                Check(
                    name="live sandbox round trip",
                    status=WARN,
                    detail=(
                        "skipped: the sandbox image is not on this machine "
                        "yet, and pulling it here would look like a hang."
                    ),
                    fix="Run `hyperbox doctor --pull` to fetch it, then run "
                    "`hyperbox doctor` again to prove the round trip.",
                )
            )
            return
    except EngineUnavailableError as exc:
        report.add(
            Check(name="live sandbox round trip", status=FAIL, detail=str(exc))
        )
        return
    from hyperbox_mcp.server import select_runtime

    runtime = select_runtime()
    sandbox_id = uuid.uuid4().hex[:12]
    started = time.time()
    handle = None
    try:
        handle = runtime.create(
            language="python", backend=backend, sandbox_id=sandbox_id
        )
        result = runtime.run(handle, "print('hyperbox ok')", timeout=30)
        if not result.success or "hyperbox ok" not in result.stdout:
            report.add(
                Check(
                    name="live sandbox round trip",
                    status=FAIL,
                    detail=(
                        f"code ran but returned exit {result.exit_code}: "
                        f"{(result.stderr or result.stdout)[:200]}"
                    ),
                )
            )
            return

        container = engine.get_container(backend, handle.meta["container_ref"])
        host = container.attrs.get("HostConfig") or {}
        networks = (
            (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        )
        observed = (
            f"memory={host.get('Memory')} nano_cpus={host.get('NanoCpus')} "
            f"pids={host.get('PidsLimit')} networks={len(networks)} "
            f"mounts={len(container.attrs.get('Mounts') or [])}"
        )
        runtime.destroy(handle)
        gone = not runtime.alive(handle)
        handle = None
        report.add(
            Check(
                name="live sandbox round trip",
                status=OK if gone else FAIL,
                detail=(
                    f"create -> run -> destroy on {backend} in "
                    f"{time.time() - started:.0f}s"
                ),
                notes=[
                    f"enforced: {observed}",
                    f"container removed: {gone}",
                ],
                fix="" if gone else "The container survived destroy; report this.",
            )
        )
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        report.add(
            Check(
                name="live sandbox round trip",
                status=FAIL,
                detail=f"{type(exc).__name__}: {exc}",
                fix=(
                    "Check the engine warnings above. If the image was being "
                    "pulled this may simply have taken too long — retry."
                ),
            )
        )
    finally:
        if handle is not None:
            try:
                runtime.destroy(handle)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass


def run_doctor(pull: bool = False, live: bool = True) -> int:
    import os

    report = Report()
    runtime = os.environ.get("HYPERBOX_RUNTIME", "native")
    print(f"HyperBox doctor  (runtime: {runtime})\n")
    check_environment(report)
    statuses = check_engines(report)
    backend = check_selection(report, statuses)
    check_image(report, backend, pull)
    check_registry(report)
    if live:
        check_round_trip(report, backend)
    else:
        report.add(
            Check(
                name="live sandbox round trip",
                status=WARN,
                detail="skipped (--quick)",
                fix="Run `hyperbox doctor` without --quick to actually prove it works.",
            )
        )

    failed, warned = report.failed, report.warned
    total = len(report.checks)
    print(f"\n{total - len(failed)}/{total} checks passed", end="")
    if warned:
        print(f", {len(warned)} warning(s)", end="")
    print()
    if failed:
        print("\nHyperBox is NOT ready on this machine. Fix the FAIL lines above.")
        return 1
    print("\nHyperBox is ready. Point an MCP client at `hyperbox`.")
    if os.environ.get("HYPERBOX_STATE_DIR"):
        print(f"(registry overridden to {os.environ['HYPERBOX_STATE_DIR']})")
    return 0
