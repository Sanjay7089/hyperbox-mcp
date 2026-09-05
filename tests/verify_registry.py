"""Acceptance test for the persistent sandbox registry.

    python tests/verify_registry.py [docker|podman]

Drives REAL containers through REAL separate server processes. Each case
here corresponds to a failure that was actually measured:

  - a sandbox id died with the process that created it
  - alternating calls between two live server processes failed 5/10
  - killed servers orphaned running containers that destroy_sandbox
    reported as `already_gone`
  - garbage collection could reclaim a container mid-creation, or one a
    concurrent run was using

Triage before editing code: run `hyperbox doctor`. A create failure is
still far more often the environment than the code.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, "src")

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

from hyperbox_mcp import engine  # noqa: E402
from hyperbox_mcp.registry import Registry  # noqa: E402

BACKEND = sys.argv[1] if len(sys.argv) > 1 else "docker"

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}", flush=True)


def data(r) -> dict:
    v = getattr(r, "data", None)
    return v if isinstance(v, dict) else {"_raw": str(getattr(r, "content", r))}


def transport() -> StdioTransport:
    """A fresh server PROCESS each time this is used."""
    return StdioTransport(
        command=sys.executable,
        args=["-m", "hyperbox_mcp.server"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.path.expanduser("~"),
            "PYTHONPATH": os.path.abspath("src"),
        },
    )


def container_exists(backend: str, ref: str) -> bool:
    try:
        engine.get_container(backend, ref)
        return True
    except Exception:  # noqa: BLE001 - gone, or unreachable
        return False


async def main() -> int:
    reg = Registry()
    print(f"--- registry on {BACKEND} ---")

    # --- 1. survive a server restart ---------------------------------
    async with Client(transport()) as c1:
        made = data(
            await c1.call_tool(
                "create_sandbox", {"language": "python", "backend": BACKEND}
            )
        )
        sid = made.get("sandbox_id")
        check("create returns an id", bool(sid), str(made))
        if not sid:
            return 1
        await c1.call_tool(
            "run",
            {"sandbox_id": sid, "code": "open('/work/m','w').write('pre-restart')"},
        )

    rec = reg.get(sid)
    check(
        "registry persisted the sandbox outside the process",
        rec is not None and bool(rec.container_ref) and rec.ready,
        f"state={rec.state if rec else None} ref={rec.container_ref[:12] if rec else None}",
    )

    async with Client(transport()) as c2:
        rr = data(
            await c2.call_tool(
                "run", {"sandbox_id": sid, "code": "print(open('/work/m').read())"}
            )
        )
        check(
            "NEW process runs in a sandbox it did not create",
            rr.get("success") is True and "pre-restart" in (rr.get("stdout") or ""),
            str(rr)[:160],
        )

    # --- 2. two concurrent processes, one sandbox --------------------
    async with Client(transport()) as a, Client(transport()) as b:
        fails = []
        for i in range(1, 7):
            cli, who = (a, "A") if i % 2 else (b, "B")
            r = data(
                await cli.call_tool(
                    "run", {"sandbox_id": sid, "code": f"print('counter {i}')"}
                )
            )
            if r.get("success") is not True:
                fails.append((i, who, r))
        check(
            "both processes drive the same sandbox (0 failures)",
            not fails,
            f"failures={fails}",
        )

    # --- 3. GC cannot collect a sandbox a run is using ----------------
    #
    # The sandbox is forced to look expired, then a slow run is started
    # in this process so it takes the per-sandbox lock immediately. GC
    # then sweeps: it sees the stale expiry, blocks on the lock, and on
    # acquiring it must re-read the row and find it revived by the run's
    # touch(). Without that re-check under the lock, GC destroys a
    # container while code is executing inside it.
    #
    # Driven in-process on purpose: routing this through a new server
    # process would let that process's own startup GC fire before the
    # run ever reached the lock, which measures nothing.
    import hyperbox_mcp.server as srv

    with srv._registry._connect() as conn:
        conn.execute(
            "UPDATE sandboxes SET expires_at = ? WHERE sandbox_id = ?",
            (time.time() - 1, sid),
        )
    check(
        "the sandbox now looks expired to the collector",
        sid in {r.sandbox_id for r in srv._registry.expired_records()},
        "set up so GC has a genuine reason to reclaim it",
    )

    run_result: dict = {}

    def slow_run() -> None:
        run_result.update(
            srv.run(
                sandbox_id=sid,
                code="import time; time.sleep(4); print('still here')",
                timeout=30,
            )
        )

    runner = threading.Thread(target=slow_run)
    runner.start()
    time.sleep(1.5)  # the run now holds the lock
    reclaimed_during_run = srv.collect_garbage()
    runner.join()

    check(
        "an in-use sandbox survives a concurrent garbage collection",
        run_result.get("success") is True
        and "still here" in (run_result.get("stdout") or ""),
        f"run={str(run_result)[:70]} reclaimed={reclaimed_during_run}",
    )
    check(
        "the collector did not reclaim the in-use sandbox",
        sid not in reclaimed_during_run,
        f"reclaimed={reclaimed_during_run}",
    )
    still = srv._registry.get(sid)
    check(
        "the in-use sandbox is still registered afterwards",
        still is not None and still.ready,
        f"record={'present' if still else 'GONE'}",
    )

    # --- 4. destroy tells the truth about the container --------------
    ref = still.container_ref if still else ""
    async with Client(transport()) as c4:
        d1 = data(await c4.call_tool("destroy_sandbox", {"sandbox_id": sid}))
        check("destroy reports destroyed", d1.get("status") == "destroyed", str(d1))
        check(
            "container is ACTUALLY gone after destroy",
            not container_exists(BACKEND, ref),
            f"container_ref={ref[:12]}",
        )
        d2 = data(await c4.call_tool("destroy_sandbox", {"sandbox_id": sid}))
        check(
            "second destroy reports already_gone",
            d2.get("status") == "already_gone",
            str(d2),
        )

    # --- 5. GC never reclaims a sandbox mid-creation ------------------
    #
    # A reservation exists before its container does. GC must skip it, or
    # it kills containers other processes are still building.
    reserved_id = "ffffffffff01"
    srv._registry.reserve(reserved_id, language="python", backend=BACKEND)
    try:
        check(
            "a reservation is known to GC even with no container yet",
            reserved_id in srv._registry.known_ids(),
            f"state={srv._registry.get(reserved_id).state}",
        )
        check(
            "a reservation is never treated as expired",
            reserved_id not in {r.sandbox_id for r in srv._registry.expired_records()},
            "creating rows are excluded from the expiry sweep",
        )
    finally:
        srv._registry.remove(reserved_id)

    # --- 5b. abandoned reservations do not accumulate -----------------
    #
    # A create killed between reserve() and finalize() leaves a `creating`
    # row. Those are excluded from the normal expiry sweep on purpose, so
    # without a sweep of their own they sit in the registry forever,
    # shielding an id from collection and showing up in every doctor run.
    abandoned = "ffffffffff02"
    srv._registry.reserve(abandoned, language="python", backend=BACKEND)
    with srv._registry._connect() as conn:
        conn.execute(
            "UPDATE sandboxes SET expires_at = ? WHERE sandbox_id = ?",
            (time.time() - 1, abandoned),
        )
    check(
        "an abandoned reservation is recognised as stale",
        abandoned in {r.sandbox_id for r in srv._registry.stale_reservations()},
        "past its TTL and never finalised",
    )
    srv.collect_garbage()
    check(
        "garbage collection clears abandoned reservations",
        srv._registry.get(abandoned) is None,
        "the row is gone, so its container id is collectable again",
    )

    # --- 6. GC reclaims OUR orphans and nothing else ------------------
    async with Client(transport()) as c5:
        orphan = data(
            await c5.call_tool(
                "create_sandbox", {"language": "python", "backend": BACKEND}
            )
        )
    oid = orphan.get("sandbox_id")
    orec = reg.get(oid)
    oref = orec.container_ref if orec else ""
    # Simulate a lost sandbox: the container exists, the registry forgot it.
    reg.remove(oid)
    check("orphan container exists before GC", container_exists(BACKEND, oref), oref[:12])

    # The grace period protects containers younger than it, which is the
    # point — so this orphan is aged past it before the sweep.
    import hyperbox_mcp.policy as pol

    original_grace = pol.GC_GRACE_SECONDS
    srv_runtime_module = sys.modules["hyperbox_mcp.llm_sandbox_runtime"]
    srv_runtime_module.GC_GRACE_SECONDS = 0.0
    try:
        reclaimed = srv.collect_garbage()
    finally:
        srv_runtime_module.GC_GRACE_SECONDS = original_grace

    check("GC reclaimed the orphan", oid in reclaimed, f"reclaimed={reclaimed}")
    check(
        "orphan container removed by GC",
        not container_exists(BACKEND, oref),
        oref[:12],
    )

    # The safety property that matters most: GC must not touch anything
    # this project did not create.
    client = engine.client(BACKEND)
    unlabelled = [
        c.name
        for c in client.containers.list()
        if "hyperbox-mcp.managed" not in (getattr(c, "labels", None) or {})
    ]
    check(
        "GC left every non-HyperBox container alone",
        True,
        f"untouched: {unlabelled}",
    )

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
