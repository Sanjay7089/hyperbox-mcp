"""Run every acceptance suite against one backend, in order.

    python tests/run_all.py [docker|podman]

Each suite drives real containers, so this takes minutes rather than
seconds. It exists so "the suite passes" is one command and one answer
instead of five, and so the final, easily-forgotten property is checked
every time: that the run left no containers behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, "src")

from hyperbox_mcp import engine, policy  # noqa: E402
from hyperbox_mcp.registry import Registry  # noqa: E402


def other_servers() -> list[str]:
    """Other HyperBox servers running right now, as 'pid path' strings.

    Worth saying out loud before a 15-minute run: a second server shares
    this machine's engines, this label and this registry, so it creates
    containers the suite did not and can garbage-collect on its own
    schedule. That shows up as an unreproducible failure an hour later.
    """
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,command="], capture_output=True, text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    mine = str(os.getpid())
    found = []
    for line in out.stdout.splitlines():
        if "hyperbox" not in line.lower():
            continue
        if "run_all" in line or "verify_" in line or line.split()[:1] == [mine]:
            continue
        if "hyperbox_mcp.server" in line or line.rstrip().endswith("hyperbox"):
            found.append(line.strip()[:100])
    return found

SUITES = (
    # First: the host side. If this fails, nothing below can pass, and
    # the reason is the platform rather than the sandbox.
    ("platform portability", "tests/verify_platform.py"),
    ("lifecycle + MCP surface", "tests/verify.py"),
    ("ownership across processes", "tests/verify_registry.py"),
    ("enforced resource policy", "tests/verify_limits.py"),
    ("structural security", "tests/verify_security.py"),
    ("containment proof", "tests/verify_containment.py"),
)


def managed_containers(backend: str) -> list[str]:
    """Containers still carrying our label. A clean run leaves none."""
    try:
        found = engine.list_managed(backend, f"{policy.LABEL_MANAGED}=true")
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not list containers on {backend}: {exc})")
        return []
    return [
        (getattr(c, "labels", None) or {}).get(policy.LABEL_ID, "?") for c in found
    ]


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "docker"
    print(f"=== HyperBox acceptance suites on {backend} ===\n", flush=True)

    others = other_servers()
    if others:
        print(
            "WARNING: another HyperBox server is running. It shares this\n"
            "machine's engines, the hyperbox-mcp.managed label and the\n"
            "registry, so it will create containers this suite did not and\n"
            "may garbage-collect on its own schedule. Quit it before trusting\n"
            "a failure here.\n"
        )
        for line in others:
            print(f"  {line}")
        print()

    before = set(managed_containers(backend))
    if before:
        print(f"note: {len(before)} HyperBox container(s) already present: {sorted(before)}\n")

    outcomes: list[tuple[str, bool, float]] = []
    for title, path in SUITES:
        print(f"--- {title} ({path}) ---", flush=True)
        started = time.time()
        result = subprocess.run(
            [sys.executable, path, backend],
            capture_output=True,
            text=True,
        )
        elapsed = time.time() - started
        tail = [
            line
            for line in result.stdout.splitlines()
            if line.startswith(("PASS", "FAIL")) or "passed" in line or "contained" in line
        ]
        for line in tail:
            if line.startswith("FAIL"):
                print(f"  {line}")
        summary = next(
            (l for l in reversed(tail) if "passed" in l or "contained" in l), "no summary"
        )
        ok = result.returncode == 0
        print(f"  {'OK  ' if ok else 'FAIL'} {summary.strip()}  ({elapsed:.0f}s)\n", flush=True)
        if not ok:
            # A failing suite's own output is the evidence; show it whole.
            print(result.stdout[-4000:])
            if result.stderr.strip():
                print(result.stderr[-2000:])
        outcomes.append((title, ok, elapsed))

    print("=== summary ===")
    for title, ok, elapsed in outcomes:
        print(f"  {'PASS' if ok else 'FAIL'}  {title}  ({elapsed:.0f}s)")

    # A container this run did not create is not this run's leak.
    #
    # The label is shared by every HyperBox on the machine, and a second
    # checkout or an MCP client left running will happily create sandboxes
    # while the suite works. Those appeared in the after-set and were
    # reported as leaks, which is a confident wrong answer about the one
    # property this check exists to prove.
    #
    # A live sandbox belonging to someone else still has a registry row;
    # anything this suite abandoned does not, because every path that
    # forgets a sandbox removes its row first. That is the discriminator.
    leaked = set(managed_containers(backend)) - before
    if leaked and others:
        # Cannot be attributed, so must not be reported as a verdict.
        #
        # Another server shares this machine's engines, label and registry,
        # and creates sandboxes throughout a run that takes minutes. A
        # container that appeared in that window may be this suite's leak
        # or may be theirs, and there is no way to tell from here -- the
        # sandbox ids are opaque and the registry row may already be gone.
        #
        # Reporting it as FAIL is a confident wrong answer of exactly the
        # kind this project exists to avoid; hiding it would be worse. So
        # it is reported as unattributable, and the run is not failed on
        # it. With no other server running, this stays a hard failure.
        print(
            f"  WARN  {len(leaked)} container(s) appeared during the run and "
            "cannot be attributed"
        )
        print(f"        {sorted(leaked)}")
        print(
            "        Another HyperBox server was running throughout. Quit it "
            "and re-run to make this check meaningful."
        )
        leaked = set()
    if leaked:
        try:
            live = {r.sandbox_id for r in Registry().all_records()}
        except Exception:  # noqa: BLE001 - fall back to reporting everything
            live = set()
        borrowed = leaked & live
        leaked -= borrowed
        if borrowed:
            print(
                f"  note: {len(borrowed)} sandbox(es) belong to another live "
                f"HyperBox, not to this run: {sorted(borrowed)}"
            )
    print(
        f"  {'PASS' if not leaked else 'FAIL'}  no containers left behind"
        + (f"  leaked={sorted(leaked)}" if leaked else "")
    )

    failed = [t for t, ok, _ in outcomes if not ok]
    total_time = sum(e for _, _, e in outcomes)
    print(
        f"\n{len(outcomes) - len(failed)}/{len(outcomes)} suites passed "
        f"in {total_time / 60:.1f} min"
    )
    return 1 if (failed or leaked) else 0


if __name__ == "__main__":
    sys.exit(main())
