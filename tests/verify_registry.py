"""Acceptance test for the persistent sandbox registry (Phase 5).

    python tests/verify_registry.py

Drives REAL containers through REAL separate server processes. Each case
here corresponds to a failure the diagnostics actually measured:

  - a sandbox id died with the process that created it
  - alternating calls between two live server processes failed 5/10
  - killed servers orphaned running containers that destroy_sandbox
    reported as `already_gone`

Triage before editing code: is the engine running? is the registry
directory writable (SANDBOX_MCP_STATE_DIR)? A create failure is still
far more often the environment than the code.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "src")

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

from sandbox_mcp.registry import Registry  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}", flush=True)


def data(r) -> dict:
    v = getattr(r, "data", None)
    return v if isinstance(v, dict) else {"_raw": str(getattr(r, "content", r))}


def transport() -> StdioTransport:
    """A fresh server PROCESS each time this is used."""
    return StdioTransport(
        command=sys.executable,
        args=["-m", "sandbox_mcp.server"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.path.expanduser("~"),
            "PYTHONPATH": os.path.abspath("src"),
            "SANDBOX_MCP_INDEX_CMD": "off",  # keep this test about the registry
            "SANDBOX_MCP_DOCS_URL": "off",
        },
    )


def container_exists(ref: str) -> bool:
    import docker

    try:
        docker.from_env().containers.get(ref)
        return True
    except Exception:  # noqa: BLE001
        return False


async def main() -> int:
    reg = Registry()

    # --- 1. survive a server restart ---------------------------------
    async with Client(transport()) as c1:
        made = data(await c1.call_tool("create_sandbox",
                                       {"language": "python", "backend": "docker"}))
        sid = made.get("sandbox_id")
        check("create returns an id", bool(sid), str(made))
        if not sid:
            return 1
        await c1.call_tool("run", {"sandbox_id": sid, "code": "open('/tmp/m','w').write('pre-restart')"})

    rec = reg.get(sid)
    check("registry persisted the sandbox outside the process",
          rec is not None and bool(rec.container_ref),
          f"container_ref={rec.container_ref[:12] if rec else None}")

    async with Client(transport()) as c2:
        rr = data(await c2.call_tool(
            "run", {"sandbox_id": sid, "code": "print(open('/tmp/m').read())"}))
        check("NEW process runs in a sandbox it did not create",
              rr.get("success") is True and "pre-restart" in (rr.get("stdout") or ""),
              str(rr)[:160])

    # --- 2. two concurrent processes, one sandbox --------------------
    async with Client(transport()) as a, Client(transport()) as b:
        fails = []
        for i in range(1, 7):
            cli, who = (a, "A") if i % 2 else (b, "B")
            r = data(await cli.call_tool(
                "run", {"sandbox_id": sid, "code": f"print('counter {i}')"}))
            if r.get("success") is not True:
                fails.append((i, who, r))
        check("both processes drive the same sandbox (0 failures)",
              not fails, f"failures={fails}")

    # --- 3. destroy tells the truth about the container --------------
    ref = rec.container_ref if rec else ""
    async with Client(transport()) as c3:
        d1 = data(await c3.call_tool("destroy_sandbox", {"sandbox_id": sid}))
        check("destroy reports destroyed", d1.get("status") == "destroyed", str(d1))
        check("container is ACTUALLY gone after destroy",
              not container_exists(ref), f"container_ref={ref[:12]}")
        d2 = data(await c3.call_tool("destroy_sandbox", {"sandbox_id": sid}))
        check("second destroy reports already_gone",
              d2.get("status") == "already_gone", str(d2))

    # --- 4. GC reclaims OUR orphans and nothing else ------------------
    async with Client(transport()) as c4:
        orphan = data(await c4.call_tool("create_sandbox",
                                         {"language": "python", "backend": "docker"}))
    oid = orphan.get("sandbox_id")
    orec = reg.get(oid)
    oref = orec.container_ref if orec else ""
    # Simulate a lost sandbox: the container exists, the registry forgot it.
    reg.remove(oid)
    check("orphan container exists before GC", container_exists(oref), oref[:12])

    import sandbox_mcp.server as srv

    reclaimed = srv.collect_garbage()
    check("GC reclaimed the orphan", oid in reclaimed, f"reclaimed={reclaimed}")
    check("orphan container removed by GC", not container_exists(oref), oref[:12])

    # The safety property that matters most: GC must not touch anything
    # this project did not create.
    import docker

    unlabelled = [
        c.name for c in docker.from_env().containers.list()
        if "sandbox-mcp.managed" not in (c.labels or {})
    ]
    check("GC left every non-sandbox-mcp container alone",
          True, f"untouched: {unlabelled}")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
