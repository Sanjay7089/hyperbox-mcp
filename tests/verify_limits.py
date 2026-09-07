"""Acceptance test for the enforced resource policy.

    python tests/verify_limits.py [docker|podman]

Every case here corresponds to something that was measured as unbounded:
a memory bomb that reached ~1.6 GB before the host OOM killer produced a
bare exit 137 with empty stderr, and `timeout=None` running for 123
seconds. Limits are server policy — an agent must not be able to raise
them, so they are asserted against the real container and against
policy.py rather than against numbers retyped here.

The engine-outage case at the end uses a genuinely dead socket rather
than a mock: a second server process is started with its engine pointed
somewhere nothing is listening, and must refuse to claim a cleanup it
cannot verify.

Safe to run: the memory bomb is bounded by the container's own cgroup
limit, well under the host's total, so it cannot pressure anything else.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, "src")

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

from hyperbox_mcp import engine, policy  # noqa: E402
from hyperbox_mcp.llm_sandbox_runtime import LLMSandboxRuntime  # noqa: E402
from hyperbox_mcp.registry import Registry  # noqa: E402

BACKEND = sys.argv[1] if len(sys.argv) > 1 else "docker"

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}", flush=True)


def data(r) -> dict:
    v = getattr(r, "data", None)
    return v if isinstance(v, dict) else {"_raw": str(getattr(r, "content", r))}


def transport(extra_env: dict | None = None) -> StdioTransport:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.path.expanduser("~"),
        "PYTHONPATH": os.path.abspath("src"),
    }
    env.update(extra_env or {})
    return StdioTransport(
        command=sys.executable, args=["-m", "hyperbox_mcp.server"], env=env
    )


NET_PROBE = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), timeout=4)\n"
    "    print('reachable')\n"
    "except Exception:\n"
    "    print('blocked')\n"
)

#: An address where nothing is listening, for both engines. Pointing a
#: server here produces a real unreachable engine, not a simulated one.
DEAD_ENGINE_ENV = {
    "DOCKER_HOST": "unix:///tmp/hyperbox-no-such-engine.sock",
    "CONTAINER_HOST": "unix:///tmp/hyperbox-no-such-engine.sock",
}


async def main() -> int:
    reg = Registry()
    print(f"--- resource policy on {BACKEND} ---")

    async with Client(transport()) as c:
        made = data(
            await c.call_tool(
                "create_sandbox", {"language": "python", "backend": BACKEND}
            )
        )
        sid = made.get("sandbox_id")
        check("create succeeds under the resource policy", bool(sid), str(made))
        if not sid:
            return 1
        rec = reg.get(sid)
        ref = rec.container_ref if rec else ""

        # --- limits are real, asserted against the engine --------------
        attrs = engine.get_container(BACKEND, ref).attrs
        host = attrs["HostConfig"]
        check(
            "memory limit applied to the container",
            host.get("Memory") == policy.MEM_LIMIT_BYTES,
            f"Memory={host.get('Memory')} expected={policy.MEM_LIMIT_BYTES}",
        )
        check(
            "CPU limit applied",
            LLMSandboxRuntime._cpu_limited(host),
            f"NanoCpus={host.get('NanoCpus')} CpuQuota={host.get('CpuQuota')} "
            f"CpuPeriod={host.get('CpuPeriod')}",
        )
        check(
            "PID limit applied",
            host.get("PidsLimit") == policy.PIDS_LIMIT,
            f"PidsLimit={host.get('PidsLimit')}",
        )
        # Scratch tmpfs mounts are expected; mounts with a host source
        # are not. See tests/verify_security.py for the full assertion.
        from_host = [
            m
            for m in (attrs.get("Mounts") or [])
            if str(m.get("Type")) != "tmpfs"
        ]
        check(
            "no host filesystem or engine socket mounted",
            not from_host and not (attrs["HostConfig"].get("Binds") or []),
            f"host-sourced mounts={from_host}",
        )

        # --- sealed by default -----------------------------------------
        r = data(await c.call_tool("run", {"sandbox_id": sid, "code": NET_PROBE}))
        check(
            "network is unreachable while caller code runs",
            "blocked" in (r.get("stdout") or ""),
            str(r.get("stdout"))[:60],
        )

        # --- build phase installs, then reseals -------------------------
        r = data(
            await c.call_tool(
                "run",
                {
                    "sandbox_id": sid,
                    "code": "import six; print('six', six.__version__)",
                    "libraries": ["six"],
                },
            )
        )
        check(
            "libraries install via the build phase",
            r.get("success") is True and "six" in (r.get("stdout") or ""),
            str(r)[:120],
        )
        r = data(await c.call_tool("run", {"sandbox_id": sid, "code": NET_PROBE}))
        check(
            "network is sealed again after the build phase",
            "blocked" in (r.get("stdout") or ""),
            str(r.get("stdout"))[:60],
        )

        # --- timeout is server policy -----------------------------------
        r = data(
            await c.call_tool(
                "run", {"sandbox_id": sid, "code": "print(1)", "timeout": None}
            )
        )
        check("timeout=None is rejected", "error" in r, str(r)[:110])

        t0 = time.time()
        r = data(
            await c.call_tool(
                "run",
                {
                    "sandbox_id": sid,
                    "code": "import time; time.sleep(300)",
                    "timeout": 600,
                },
            )
        )
        elapsed = time.time() - t0
        check(
            "oversized timeout is clamped to the server cap",
            elapsed < (policy.MAX_TIMEOUT_SECONDS * 2) and r.get("success") is False,
            f"elapsed={elapsed:.1f}s cap={policy.MAX_TIMEOUT_SECONDS:g}s "
            f"timed_out={r.get('timed_out')}",
        )

        # --- output cap --------------------------------------------------
        r = data(
            await c.call_tool(
                "run",
                {"sandbox_id": sid, "code": "print('x' * 500000)", "timeout": 30},
            )
        )
        out = r.get("stdout") or ""
        check(
            "huge stdout is capped and marked truncated",
            len(out) < policy.MAX_OUTPUT_CHARS + 5_000 and "truncated" in out,
            f"len={len(out)} cap={policy.MAX_OUTPUT_CHARS}",
        )

        # --- scratch space is bounded ------------------------------------
        r = data(
            await c.call_tool(
                "run",
                {
                    "sandbox_id": sid,
                    "code": (
                        "try:\n"
                        "    with open('/work/big', 'wb') as f:\n"
                        "        for _ in range(400):\n"
                        "            f.write(b'x' * 1_000_000)\n"
                        "    print('wrote 400MB unchecked')\n"
                        "except OSError as e:\n"
                        "    print('BOUNDED:', e.__class__.__name__)\n"
                    ),
                    "timeout": 60,
                },
            )
        )
        check(
            "the writable scratch directory is size-limited",
            "BOUNDED" in (r.get("stdout") or "")
            or r.get("success") is False,
            f"stdout={(r.get('stdout') or '')[:70]!r}",
        )

        # --- memory bomb, bounded by the container -----------------------
        r = data(
            await c.call_tool(
                "run",
                {
                    "sandbox_id": sid,
                    "code": (
                        "a = []\n"
                        "while True:\n"
                        "    a.append(bytearray(10_000_000))\n"
                    ),
                    "timeout": 60,
                },
            )
        )
        check(
            "memory bomb is killed and the reason is legible (not a bare 137)",
            r.get("success") is False and "memory" in (r.get("stderr") or "").lower(),
            f"exit={r.get('exit_code')} stderr={(r.get('stderr') or '')[:110]}",
        )

    # --- an unreachable engine is never reported as a clean cleanup ----
    #
    # A second server process, with its engine pointed at a socket that
    # does not exist. It must refuse to say the sandbox is gone, and must
    # leave the registry row alone so the container is not forgotten.
    async with Client(transport(DEAD_ENGINE_ENV)) as dead:
        d = data(await dead.call_tool("destroy_sandbox", {"sandbox_id": sid}))
        check(
            "destroy against an unreachable engine returns an error, not success",
            "error" in d and d.get("status") != "already_gone",
            str(d)[:140],
        )
        check(
            "the error names the fix rather than just failing",
            "error" in d
            and any(
                hint in (
                    d.get("error_message", "")
                    + " "
                    + (d.get("error") or {}).get("fix", "")
                ).lower()
                for hint in ("docker info", "systemctl", "docker desktop", "podman")
            ),
            str(d.get("error", ""))[:120],
        )

    survivor = reg.get(sid)
    check(
        "the sandbox is still on file after the failed destroy",
        survivor is not None,
        "an unreachable container has not been proven gone",
    )
    check(
        "and its container is genuinely still running",
        engine.get_container(BACKEND, ref).status == "running",
        f"container_ref={ref[:12]}",
    )

    # Clean up through a healthy engine.
    async with Client(transport()) as healthy:
        final = data(
            await healthy.call_tool("destroy_sandbox", {"sandbox_id": sid})
        )
        check(
            "a healthy engine then destroys it for real",
            final.get("status") == "destroyed",
            str(final),
        )

    failed = [f for f in results if not f[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
