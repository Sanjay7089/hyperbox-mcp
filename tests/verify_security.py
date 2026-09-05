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
import sys

sys.path.insert(0, "src")

from fastmcp import Client  # noqa: E402

from hyperbox_mcp import engine, policy, server  # noqa: E402

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
            check(
                "CPU limit is applied to the container",
                host.get("NanoCpus") == policy.NANO_CPUS,
                f"NanoCpus={host.get('NanoCpus')} expected={policy.NANO_CPUS}",
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
            mounts = attrs.get("Mounts") or []
            binds = host.get("Binds") or []
            check(
                "no host filesystem is mounted",
                not mounts and not binds,
                f"Mounts={mounts} Binds={binds}",
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
            tmpfs = host.get("Tmpfs") or {}
            check(
                "scratch space is tmpfs and size-limited",
                all(
                    path in tmpfs and policy.TMPFS_SIZE in str(tmpfs.get(path))
                    for path in policy.TMPFS
                ),
                f"Tmpfs={tmpfs}",
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
            # docs/security-model.md from claiming otherwise.
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

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
