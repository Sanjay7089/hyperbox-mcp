"""Structural proof that a sandbox is configured the way we describe it.

    python tests/verify_security.py [docker|podman]

The containment suite proves behaviour: hostile code tried something and
failed. This suite proves configuration: it reads the real container's
attributes back off the engine and asserts each control is actually
present. The two answer different questions, and only this one catches a
control that is absent but happens not to be exercised — a sandbox whose
network probe fails for an unrelated reason still looks contained.

Nothing here is asserted against our own constants alone; every value is
compared to what the engine reports about a container that exists.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, "src")

from fastmcp import Client  # noqa: E402

from hyperbox_mcp import engine, policy, sandbox_ops, server  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}", flush=True)
    if detail:
        print(f"        {detail}")


def networks_of(attrs: dict) -> dict:
    return (attrs.get("NetworkSettings") or {}).get("Networks") or {}


async def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "docker"
    print(f"--- structural security on {backend} ---\n")

    async with Client(server.mcp) as client:
        made = (
            await client.call_tool(
                "create_sandbox", {"language": "python", "backend": backend}
            )
        ).data
        if not isinstance(made, dict) or "sandbox_id" not in made:
            check("sandbox created for inspection", False, str(made))
            return 1
        sid = made["sandbox_id"]
        print(f"sandbox: {sid} on {made.get('backend')}\n")

        rec = server._registry.get(sid)
        container = engine.get_container(rec.backend, rec.container_ref)
        container.reload()
        attrs = container.attrs
        host = attrs.get("HostConfig") or {}
        config = attrs.get("Config") or {}

        try:
            # --- resource ceilings, as the engine records them ---------
            check(
                "memory limit is applied to the container",
                host.get("Memory") == policy.MEM_LIMIT_BYTES,
                f"Memory={host.get('Memory')} expected={policy.MEM_LIMIT_BYTES}",
            )
            # Docker records the ceiling as NanoCpus, Podman as a quota
            # over a period. Podman-py silently DISCARDS nano_cpus, so
            # asserting only docker's spelling would pass a podman
            # container that has no CPU limit whatsoever.
            check(
                "a CPU ceiling is genuinely in force",
                sandbox_ops.cpu_limited(host),
                f"NanoCpus={host.get('NanoCpus')} "
                f"CpuQuota={host.get('CpuQuota')} "
                f"CpuPeriod={host.get('CpuPeriod')} "
                f"(expected {policy.CPUS} CPU)",
            )
            check(
                "PID limit is applied to the container",
                host.get("PidsLimit") == policy.PIDS_LIMIT,
                f"PidsLimit={host.get('PidsLimit')} expected={policy.PIDS_LIMIT}",
            )

            # --- the network is gone, not merely unreachable ------------
            check(
                "zero networks are attached after sealing",
                len(networks_of(attrs)) == 0,
                f"Networks={list(networks_of(attrs))}",
            )

            # --- nothing of the host is inside -------------------------
            #
            # The claim is not "no mounts" — the scratch tmpfs is a mount,
            # and podman lists it here. The claim is that no mount reaches
            # anything on the host: a tmpfs has no host source, a bind
            # does.
            mounts = attrs.get("Mounts") or []
            binds = host.get("Binds") or []
            host_sourced = [
                m
                for m in mounts
                if str(m.get("Type")) not in {"tmpfs", ""}
                or str(m.get("Source", "")).startswith("/")
                and str(m.get("Type")) != "tmpfs"
            ]
            check(
                "no mount reaches the host filesystem",
                not host_sourced and not binds,
                f"host-sourced={host_sourced} Binds={binds} "
                f"(of {len(mounts)} mount(s) total)",
            )
            blob = f"{mounts}{binds}"
            check(
                "no container engine socket is mounted",
                "docker.sock" not in blob and "podman.sock" not in blob,
                f"searched Mounts+Binds ({len(blob)} chars)",
            )

            # --- escalation is closed off -------------------------------
            check(
                "container is not privileged",
                host.get("Privileged") in (False, None),
                f"Privileged={host.get('Privileged')}",
            )
            check(
                "no extra capabilities are added",
                not (host.get("CapAdd") or []),
                f"CapAdd={host.get('CapAdd')}",
            )
            sec_opt = host.get("SecurityOpt") or []
            check(
                "no-new-privileges is set",
                any("no-new-privileges" in str(o) for o in sec_opt),
                f"SecurityOpt={sec_opt}",
            )

            # --- bounded writable space ---------------------------------
            #
            # The two engines record this differently: docker reports a
            # HostConfig.Tmpfs mapping, podman reports tmpfs entries under
            # Mounts. Rather than trust either spelling, the size limit is
            # also proved from inside the container below.
            tmpfs = host.get("Tmpfs") or {}
            declared = {
                m.get("Destination") or m.get("Target")
                for m in (attrs.get("Mounts") or [])
                if str(m.get("Type")) == "tmpfs"
            }
            check(
                "scratch space is declared tmpfs at every documented path",
                all(
                    path in tmpfs or path in declared for path in policy.TMPFS_PATHS
                ),
                f"Tmpfs={tmpfs} tmpfs-mounts={sorted(declared)}",
            )
            sized = (
                await client.call_tool(
                    "run",
                    {
                        "sandbox_id": sid,
                        "code": (
                            "import subprocess\n"
                            "print(subprocess.run(['df','-h','/work'],"
                            " capture_output=True, text=True).stdout)\n"
                        ),
                        "timeout": 30,
                    },
                )
            ).data
            df = (sized.get("stdout") or "").strip()
            check(
                "the scratch mount really is a small tmpfs inside the container",
                "tmpfs" in df.lower() and "64M" in df.upper().replace("MB", "M"),
                f"df -h /work: {df.splitlines()[-1] if df else '(no output)'}",
            )

            # --- ownership, so GC can tell ours from everyone else's ----
            labels = config.get("Labels") or {}
            check(
                "both HyperBox labels are present and carry this id",
                labels.get(policy.LABEL_MANAGED) == "true"
                and labels.get(policy.LABEL_ID) == sid,
                f"{policy.LABEL_MANAGED}={labels.get(policy.LABEL_MANAGED)} "
                f"{policy.LABEL_ID}={labels.get(policy.LABEL_ID)}",
            )

            # --- the user the code actually runs as ---------------------
            #
            # Recorded, not demanded. A non-root user was measured to
            # break this backend (llm-sandbox builds a venv under
            # /sandbox during setup, which needs root in these images),
            # so the honest test asserts what is observed and keeps
            # docs/security.md from claiming otherwise.
            probe = (
                await client.call_tool(
                    "run",
                    {
                        "sandbox_id": sid,
                        "code": "import os; print('uid', os.getuid())",
                        "timeout": 30,
                    },
                )
            ).data
            reported_uid = (probe.get("stdout") or "").strip()
            check(
                "the user code runs as is recorded, not assumed",
                bool(reported_uid),
                f"Config.User={config.get('User')!r}; inside: {reported_uid!r}",
            )

            # --- re-sealing after a dependency install is real ----------
            installed = (
                await client.call_tool(
                    "run",
                    {
                        "sandbox_id": sid,
                        "code": "import six; print('six', six.__version__)",
                        "libraries": ["six"],
                        "timeout": 60,
                    },
                )
            ).data
            check(
                "a declared library installs during the build phase",
                installed.get("success") is True,
                str(installed)[:140],
            )
            container.reload()
            check(
                "zero networks are attached again after the install",
                len(networks_of(container.attrs)) == 0,
                f"Networks={list(networks_of(container.attrs))}",
            )

            # --- the install window accepts names, not flags ------------
            refused = (
                await client.call_tool(
                    "run",
                    {
                        "sandbox_id": sid,
                        "code": "print('should not run')",
                        "libraries": ["--index-url http://example.invalid/simple"],
                    },
                )
            ).data
            check(
                "an installer flag disguised as a package is refused",
                "error" in refused,
                str(refused)[:140],
            )
        finally:
            gone = (
                await client.call_tool("destroy_sandbox", {"sandbox_id": sid})
            ).data
            check(
                "sandbox is destroyed cleanly afterwards",
                gone.get("status") == "destroyed",
                str(gone),
            )

    # --- a networked environment says so, and a sealed one is sealed ---
    #
    # `--allow-network` is the one way a sandbox keeps its network, and it
    # is a property of an environment a HUMAN built, never a tool
    # parameter. What matters is that the description and the behaviour
    # agree in BOTH directions: a sandbox that can reach the internet
    # while something calls it sealed is the worst outcome available
    # here, and so is refusing an environment the user deliberately
    # opened.
    import tempfile

    from hyperbox_mcp.builder import _write_manifest

    env_root = Path(tempfile.mkdtemp(prefix="hyperbox-netenv-"))
    real_env_dir = os.environ.get("HYPERBOX_ENV_DIR")
    os.environ["HYPERBOX_ENV_DIR"] = str(env_root)
    try:
        image = server._runtime.image_for("python")
        # Written directly rather than built: this asserts how the flag is
        # HONOURED, and a pull would only re-test the builder.
        _write_manifest("netopen", image, "image", backend, allow_network=True)
        _write_manifest("netsealed", image, "image", backend, allow_network=False)

        probe = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), 5)\n"
            "    print('REACHABLE')\n"
            "except Exception:\n"
            "    print('SEALED')\n"
        )
        async with Client(server.mcp) as client:
            for env, expect in (("netopen", "REACHABLE"), ("netsealed", "SEALED")):
                made = (
                    await client.call_tool(
                        "create_sandbox",
                        {"language": "python", "backend": backend,
                         "environment": env},
                    )
                ).data
                if "error" in made:
                    check(f"environment '{env}' can be created", False,
                          str(made)[:140])
                    continue
                sid = made["sandbox_id"]
                try:
                    said_open = "CAN reach the internet" in made["network"]
                    got = (
                        await client.call_tool(
                            "run", {"sandbox_id": sid, "code": probe}
                        )
                    ).data["stdout"].strip()
                    check(
                        f"'{env}' behaves the way its result describes it",
                        got == expect and said_open == (expect == "REACHABLE"),
                        f"result says {'open' if said_open else 'sealed'}, "
                        f"probe says {got}, expected {expect}",
                    )
                finally:
                    await client.call_tool("destroy_sandbox",
                                           {"sandbox_id": sid})
    finally:
        if real_env_dir is None:
            os.environ.pop("HYPERBOX_ENV_DIR", None)
        else:
            os.environ["HYPERBOX_ENV_DIR"] = real_env_dir

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
