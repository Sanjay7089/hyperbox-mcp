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

    # --- sync_from never carries a secret in, and says what it dropped -
    #
    # Shipped in 0.4.0 with no coverage. The denylist is the whole safety
    # argument for letting host files into a sandbox at all, and until now
    # nothing had ever put a `.env` next to a source file and checked
    # which one arrived.
    import tempfile as _tempfile

    tree = Path(_tempfile.mkdtemp(prefix="hyperbox-sync-"))
    (tree / "app.py").write_text("SECRET = 'not-really'\n", encoding="utf-8")
    (tree / ".env").write_text("AWS_SECRET_ACCESS_KEY=hunter2\n",
                               encoding="utf-8")
    (tree / ".npmrc").write_text("//registry:_authToken=hunter2\n",
                                 encoding="utf-8")
    (tree / ".ssh").mkdir()
    (tree / ".ssh" / "id_ed25519").write_text("PRIVATE KEY\n",
                                              encoding="utf-8")
    outside = Path(_tempfile.mkdtemp(prefix="hyperbox-outside-"))
    (outside / "loot.txt").write_text("host file\n", encoding="utf-8")
    try:
        (tree / "escape").symlink_to(outside / "loot.txt")
    except OSError:
        pass  # symlinks may need privilege on Windows; the rest still holds

    real_roots = os.environ.get("HYPERBOX_SYNC_ROOTS")
    os.environ["HYPERBOX_SYNC_ROOTS"] = str(tree)
    try:
        async with Client(server.mcp) as client:
            made = (
                await client.call_tool(
                    "create_sandbox",
                    {"language": "python", "backend": backend,
                     "sync_from": str(tree)},
                )
            ).data
            if "error" in made:
                check("a directory can be synced in", False, str(made)[:140])
            else:
                sid = made["sandbox_id"]
                try:
                    listing = (
                        await client.call_tool(
                            "run",
                            {"sandbox_id": sid,
                             "code": "import os\n"
                                     "for r, d, f in os.walk('/sandbox'):\n"
                                     "    for n in f: print(os.path.join(r, n))\n"},
                        )
                    ).data["stdout"]
                    arrived = {Path(line).name
                               for line in listing.splitlines() if line.strip()}
                    leaked = arrived & {".env", ".npmrc", "id_ed25519",
                                        "loot.txt", "escape"}
                    check(
                        "no denylisted secret reaches the sandbox",
                        not leaked,
                        f"leaked: {sorted(leaked)}" if leaked
                        else f"arrived: {sorted(arrived)}",
                    )
                    check(
                        "the ordinary file it was asked for does arrive",
                        "app.py" in arrived,
                        f"arrived: {sorted(arrived)}",
                    )
                    # Reported, not silently dropped -- an agent that
                    # cannot see what was withheld will debug the wrong
                    # thing when its code cannot find a config file.
                    skipped = made.get("sync", {}).get("skipped", [])
                    names = {Path(s.get("path", "")).name for s in skipped}
                    check(
                        "and the manifest names what it withheld, with a reason",
                        {".env", ".npmrc"} <= names
                        and all(s.get("reason") for s in skipped),
                        f"skipped: {skipped}"[:160],
                    )
                finally:
                    await client.call_tool("destroy_sandbox",
                                           {"sandbox_id": sid})

            # The boundary itself: a directory nobody allowed is refused
            # before any container is involved.
            refused = (
                await client.call_tool(
                    "create_sandbox",
                    {"language": "python", "backend": backend,
                     "sync_from": str(outside)},
                )
            ).data
            check(
                "a directory outside every allowed root is refused",
                "error" in refused
                and refused["error"].get("code") == "INVALID_INPUT",
                str(refused)[:120],
            )
    finally:
        if real_roots is None:
            os.environ.pop("HYPERBOX_SYNC_ROOTS", None)
        else:
            os.environ["HYPERBOX_SYNC_ROOTS"] = real_roots

    # --- a pre-0.4.0 "open" environment is sealed anyway ---
    #
    # `--allow-network` was removed in 0.4.0, but manifests written by
    # older versions still sit in ~/.hyperbox/environments carrying
    # {"network": "bridge"}. Nothing reads that key any more, and this is
    # what proves it: the assertion is not that the code was deleted but
    # that a sandbox built from such an environment cannot reach the
    # internet. Deleting the old test and stopping there would have left
    # the removal unverified against the one input that used to trigger it.
    import json
    import tempfile

    from hyperbox_mcp.builder import _write_manifest

    env_root = Path(tempfile.mkdtemp(prefix="hyperbox-netenv-"))
    real_env_dir = os.environ.get("HYPERBOX_ENV_DIR")
    os.environ["HYPERBOX_ENV_DIR"] = str(env_root)
    try:
        image = server._runtime.image_for("python")
        _write_manifest("legacyopen", image, "image", backend)
        # Put the field back by hand, exactly as a pre-0.4.0 build wrote
        # it. _write_manifest no longer emits it, so this is the only way
        # to reproduce the input that mattered.
        legacy = env_root / "legacyopen" / "env.json"
        manifest = json.loads(legacy.read_text(encoding="utf-8"))
        manifest["network"] = "bridge"
        legacy.write_text(json.dumps(manifest, indent=2) + "\n",
                          encoding="utf-8")

        probe = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), 5)\n"
            "    print('REACHABLE')\n"
            "except Exception:\n"
            "    print('SEALED')\n"
        )
        async with Client(server.mcp) as client:
            made = (
                await client.call_tool(
                    "create_sandbox",
                    {"language": "python", "backend": backend,
                     "environment": "legacyopen"},
                )
            ).data
            if "error" in made:
                check("a legacy 'open' environment can still be created",
                      False, str(made)[:140])
            else:
                sid = made["sandbox_id"]
                try:
                    got = (
                        await client.call_tool(
                            "run", {"sandbox_id": sid, "code": probe}
                        )
                    ).data["stdout"].strip()
                    check(
                        "a pre-0.4.0 network:bridge environment is sealed anyway",
                        got == "SEALED",
                        f"probe says {got}, expected SEALED",
                    )
                    check(
                        "and its result describes it as sealed",
                        "cannot reach the internet" in made["network"],
                        str(made.get("network")),
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
