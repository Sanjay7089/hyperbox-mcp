"""Run every acceptance suite against one backend, in order.

    python tests/run_all.py [docker|podman]

Each suite drives real containers, so this takes minutes rather than
seconds. It exists so "the suite passes" is one command and one answer
instead of five, and so the final, easily-forgotten property is checked
every time: that the run left no containers behind.
"""

from __future__ import annotations

import subprocess
import sys
import time

sys.path.insert(0, "src")

from hyperbox_mcp import engine, policy  # noqa: E402

SUITES = (
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
    print(f"=== HyperBox acceptance suites on {backend} ===\n")

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
        print(f"  {'OK  ' if ok else 'FAIL'} {summary.strip()}  ({elapsed:.0f}s)\n")
        if not ok:
            # A failing suite's own output is the evidence; show it whole.
            print(result.stdout[-4000:])
            if result.stderr.strip():
                print(result.stderr[-2000:])
        outcomes.append((title, ok, elapsed))

    print("=== summary ===")
    for title, ok, elapsed in outcomes:
        print(f"  {'PASS' if ok else 'FAIL'}  {title}  ({elapsed:.0f}s)")

    leaked = set(managed_containers(backend)) - before
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
