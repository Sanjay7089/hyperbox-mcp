"""Proof that a HyperBox sandbox actually contains hostile code.

    python tests/verify_containment.py [docker|podman]

Every check runs REAL code in a REAL container and prints what actually
came back. This is the evidence behind the project's central claim — that
an agent can run unreviewed code here instead of on your machine — so it
is deliberately written to be quotable rather than merely green.

Each probe attempts something that WOULD be damaging on the host, and
asserts the sandbox stopped it.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys

sys.path.insert(0, "src")

from fastmcp import Client  # noqa: E402

from hyperbox_mcp import server  # noqa: E402

BACKEND = sys.argv[1] if len(sys.argv) > 1 else "docker"

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}")
    if detail:
        print(f"        {detail}")


# A file that certainly exists on the host, addressed absolutely. If the
# sandbox could read the host filesystem, this would succeed.
#
# This probe is weaker on Windows than it looks: the path is `C:\...`,
# which a Linux container could never resolve whatever its mounts were,
# so it proves the claim only on POSIX hosts. The account-name probe
# below holds everywhere, and tests/verify_security.py asserts the real
# guarantee structurally — that no mount reaches the host at all.
HOST_FILE = os.path.abspath("pyproject.toml")

# The name of the account running these tests. It appears in the host's
# user database and must NOT appear in the container's.
HOST_USER = getpass.getuser()

PROBES = {
    "read the host filesystem": (
        f"try:\n"
        f"    print(open({HOST_FILE!r}).read()[:40])\n"
        f"except Exception as e:\n"
        f"    print('DENIED:', type(e).__name__)\n"
    ),
    "read the host user database": (
        "try:\n"
        "    print(open('/etc/passwd').read())\n"
        "except Exception as e:\n"
        "    print('DENIED:', type(e).__name__)\n"
    ),
    "reach the network": (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
        "    print('REACHED THE INTERNET')\n"
        "except Exception as e:\n"
        "    print('DENIED:', type(e).__name__)\n"
    ),
    "reach the container engine socket": (
        "import os\n"
        "socks = ['/var/run/docker.sock', '/run/docker.sock',\n"
        "         '/var/run/podman/podman.sock', '/run/podman/podman.sock']\n"
        "print('engine sockets present:', [s for s in socks if os.path.exists(s)])\n"
    ),
    "exhaust host memory": (
        "a = []\n"
        "while True:\n"
        "    a.append(bytearray(10_000_000))\n"
    ),
    "fork-bomb the host": (
        "import os\n"
        "n = 0\n"
        "try:\n"
        "    for _ in range(400):\n"
        "        if os.fork() == 0:\n"
        "            os._exit(0)\n"
        "        n += 1\n"
        "except Exception as e:\n"
        "    print('DENIED after', n, 'processes:', type(e).__name__)\n"
        "else:\n"
        "    print('spawned', n, 'processes unchecked')\n"
    ),
}


async def main() -> int:
    async with Client(server.mcp) as client:
        made = (await client.call_tool(
            "create_sandbox", {"language": "python", "backend": BACKEND})).data
        if not isinstance(made, dict) or "sandbox_id" not in made:
            check("sandbox created for containment probes", False, str(made))
            return 1
        sid = made["sandbox_id"]
        print(f"sandbox: {sid} on {made.get('backend')}\n")

        async def probe(name: str, timeout: float = 60) -> dict:
            r = await client.call_tool(
                "run", {"sandbox_id": sid, "code": PROBES[name], "timeout": timeout})
            return r.data

        r = await probe("read the host filesystem")
        out = (r.get("stdout") or "").strip()
        check("a host path is unreachable from inside",
              out.startswith("DENIED"), f"stdout: {out!r}")

        # Reading /etc/passwd inside a container proves nothing on its
        # own — every Linux image ships one. What matters is WHOSE it is.
        # The host's account name must not appear in it.
        r = await probe("read the host user database")
        out = (r.get("stdout") or "")
        accounts = [
            line.split(":", 1)[0]
            for line in out.splitlines()
            if ":" in line
        ]
        check("the user database inside is the container's, not the host's",
              HOST_USER not in accounts,
              f"host account {HOST_USER!r} absent; container has "
              f"{len(accounts)} accounts e.g. {accounts[:4]}")

        r = await probe("reach the network")
        out = (r.get("stdout") or "").strip()
        check("network is unreachable from inside",
              out.startswith("DENIED"), f"stdout: {out!r}")

        r = await probe("reach the container engine socket")
        out = (r.get("stdout") or "").strip()
        check("container engine socket is not mounted",
              out.endswith("[]"), f"stdout: {out!r}")

        r = await probe("exhaust host memory")
        err = (r.get("stderr") or "").strip()
        check("memory exhaustion is capped, with a legible reason",
              r.get("success") is False and "memory" in err.lower(),
              f"exit_code: {r.get('exit_code')} | stderr: {err[:110]}")

        r = await probe("fork-bomb the host")
        out = (r.get("stdout") or "").strip()
        err = (r.get("stderr") or "").strip()
        contained = "DENIED" in out or r.get("success") is False
        check("process explosion is capped by the PID limit",
              contained, f"stdout: {out[:90]!r} exit_code: {r.get('exit_code')}")

        # The host is unchanged, and cleanup is honest.
        gone = (await client.call_tool("destroy_sandbox", {"sandbox_id": sid})).data
        check("sandbox is destroyed cleanly afterwards",
              gone.get("status") == "destroyed", str(gone))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} contained")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
