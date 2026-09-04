"""Acceptance test for the enforced resource policy.

    python tests/verify_limits.py

Every case here corresponds to something the diagnostics measured as
unbounded: a memory bomb that reached ~1.6 GB before the VM's OOM killer
produced a bare exit 137 with empty stderr, and `timeout=None` running
for 123 seconds. Limits are server policy — an agent must not be able to
raise them, so they are asserted against the real container's HostConfig
rather than against our own constants.

Safe to run: the memory bomb is now bounded by the container's own cgroup
limit, well under the Docker VM's total, so it cannot pressure other
containers on the machine.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, "src")

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

from hyperbox_mcp.llm_sandbox_runtime import MEM_LIMIT, NANO_CPUS, PIDS_LIMIT  # noqa: E402
from hyperbox_mcp.registry import Registry  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}", flush=True)


def data(r) -> dict:
    v = getattr(r, "data", None)
    return v if isinstance(v, dict) else {"_raw": str(getattr(r, "content", r))}


def transport() -> StdioTransport:
    return StdioTransport(
        command=sys.executable,
        args=["-m", "hyperbox_mcp.server"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.path.expanduser("~"),
            "PYTHONPATH": os.path.abspath("src"),
            "HYPERBOX_MCP_INDEX_CMD": "off",
            "HYPERBOX_MCP_DOCS_URL": "off",
        },
    )


NET_PROBE = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), timeout=4)\n"
    "    print('reachable')\n"
    "except Exception:\n"
    "    print('blocked')\n"
)


async def main() -> int:
    reg = Registry()
    async with Client(transport()) as c:
        made = data(await c.call_tool("create_sandbox",
                                      {"language": "python", "backend": "docker"}))
        sid = made.get("sandbox_id")
        check("create succeeds under the resource policy", bool(sid), str(made))
        if not sid:
            return 1
        rec = reg.get(sid)
        ref = rec.container_ref if rec else ""

        # --- limits are real, asserted against the engine --------------
        import docker

        attrs = docker.from_env().containers.get(ref).attrs
        host = attrs["HostConfig"]
        check("memory limit applied to the container",
              host.get("Memory") == 1073741824, f"Memory={host.get('Memory')}")
        check("CPU limit applied", host.get("NanoCpus") == NANO_CPUS,
              f"NanoCpus={host.get('NanoCpus')}")
        check("PID limit applied", host.get("PidsLimit") == PIDS_LIMIT,
              f"PidsLimit={host.get('PidsLimit')}")
        check("no host filesystem or engine socket mounted",
              not attrs.get("Mounts"), f"Mounts={attrs.get('Mounts')}")

        # --- sealed by default -----------------------------------------
        r = data(await c.call_tool("run", {"sandbox_id": sid, "code": NET_PROBE}))
        check("network is unreachable while caller code runs",
              "blocked" in (r.get("stdout") or ""), str(r.get("stdout"))[:60])

        # --- build phase installs, then reseals -------------------------
        r = data(await c.call_tool("run", {
            "sandbox_id": sid,
            "code": "import six; print('six', six.__version__)",
            "libraries": ["six"]}))
        check("libraries install via the build phase",
              r.get("success") is True and "six" in (r.get("stdout") or ""),
              str(r)[:120])
        r = data(await c.call_tool("run", {"sandbox_id": sid, "code": NET_PROBE}))
        check("network is sealed again after the build phase",
              "blocked" in (r.get("stdout") or ""), str(r.get("stdout"))[:60])

        # --- timeout is server policy -----------------------------------
        r = data(await c.call_tool("run",
                                   {"sandbox_id": sid, "code": "print(1)", "timeout": None}))
        check("timeout=None is rejected", "error" in r, str(r)[:110])

        t0 = time.time()
        r = data(await c.call_tool("run", {
            "sandbox_id": sid,
            "code": "import time; time.sleep(300)", "timeout": 600}))
        elapsed = time.time() - t0
        check("oversized timeout is clamped to the server cap",
              elapsed < 120 and r.get("success") is False,
              f"elapsed={elapsed:.1f}s timed_out_reported={r.get('exit_code')}")

        # --- output cap --------------------------------------------------
        r = data(await c.call_tool("run", {
            "sandbox_id": sid, "code": "print('x' * 500000)", "timeout": 30}))
        out = r.get("stdout") or ""
        check("huge stdout is capped and marked truncated",
              len(out) < 25_000 and "truncated" in out,
              f"len={len(out)}")

        # --- memory bomb, now bounded by the container -------------------
        r = data(await c.call_tool("run", {
            "sandbox_id": sid,
            "code": ("a = []\n"
                     "while True:\n"
                     "    a.append(bytearray(10_000_000))\n"),
            "timeout": 60}))
        check("memory bomb is killed and the reason is legible (not a bare 137)",
              r.get("success") is False and "memory" in (r.get("stderr") or "").lower(),
              f"exit={r.get('exit_code')} stderr={(r.get('stderr') or '')[:120]}")

        await c.call_tool("destroy_sandbox", {"sandbox_id": sid})

    failed = [f for f in results if not f[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
