"""Platform portability checks — the parts that differ off Linux/macOS.

    python tests/verify_platform.py

Everything here is about the HOST side: file locking, engine discovery,
transport selection and path handling. It needs no container for most
cases, so it is the first thing to run on a machine HyperBox has never
been tried on. `hyperbox doctor` covers the container round trip.

This exists because the code has POSIX habits that are easy to
reintroduce: `fcntl`, `os.getuid`, unix socket paths. Each one of those
is a crash on Windows rather than a degradation, so they get a test.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, "src")

from hyperbox_mcp import engine, policy  # noqa: E402
from hyperbox_mcp.filelock import WINDOWS, FileLock  # noqa: E402
from hyperbox_mcp.registry import Registry  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}", flush=True)
    if detail:
        print(f"        {detail}")


def main() -> int:
    print(f"--- platform checks on {platform.system()} {platform.machine()} ---")
    print(f"    Python {platform.python_version()}, "
          f"lock backend: {'msvcrt' if WINDOWS else 'fcntl'}\n")

    # --- 1. the package imports at all ------------------------------
    #
    # On Windows this is the whole ballgame: a POSIX-only import here
    # means the MCP server cannot start, and the client shows no tools
    # with no useful error.
    try:
        import hyperbox_mcp.server  # noqa: F401

        check("the MCP server module imports on this platform", True)
    except Exception as exc:  # noqa: BLE001
        check(
            "the MCP server module imports on this platform",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        return summarize()

    # --- 2. the lock actually excludes, in separate processes --------
    lock_path = Path(tempfile.mkdtemp()) / "portable.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.path.insert(0,'src');"
            "from hyperbox_mcp.filelock import FileLock;"
            f"lock=FileLock(r'{lock_path}')\n"
            "with lock:\n"
            "    print('held', flush=True); time.sleep(3)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    holder.stdout.readline()  # wait until the child really holds it
    started = time.time()
    with FileLock(lock_path, timeout=30):
        waited = time.time() - started
    holder.wait(timeout=30)
    check(
        "a lock held by another PROCESS blocks this one",
        waited > 1.5,
        f"waited {waited:.1f}s for a lock the child held for 3s",
    )

    # --- 3. the lock survives its holder being killed ----------------
    #
    # The property that matters most: a server killed mid-operation must
    # not wedge a sandbox forever. Both platforms get this from the OS
    # releasing the handle, not from cleanup code.
    victim = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.path.insert(0,'src');"
            "from hyperbox_mcp.filelock import FileLock;"
            f"lock=FileLock(r'{lock_path}')\n"
            "with lock:\n"
            "    print('held', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert victim.stdout is not None
    victim.stdout.readline()
    victim.kill()
    victim.wait(timeout=15)
    started = time.time()
    try:
        with FileLock(lock_path, timeout=20):
            reclaimed = True
    except Exception as exc:  # noqa: BLE001
        reclaimed = False
        print(f"        {type(exc).__name__}: {exc}")
    check(
        "a killed holder's lock is released by the OS",
        reclaimed,
        f"reclaimed in {time.time() - started:.1f}s after SIGKILL",
    )

    # --- 4. state lives somewhere writable, off the repo -------------
    try:
        reg = Registry()
        reg.reserve("ffffffffffff", language="python", backend="docker")
        found = reg.get("ffffffffffff")
        reg.remove("ffffffffffff")
        ok = found is not None and not found.ready
    except Exception as exc:  # noqa: BLE001
        ok, found = False, None
        print(f"        {type(exc).__name__}: {exc}")
    check(
        "the registry opens and round-trips a record",
        ok,
        f"state dir: {Registry().dir}",
    )

    # --- 5. engine discovery, without POSIX assumptions --------------
    statuses = {b: engine.probe(b) for b in policy.BACKENDS}
    for backend, status in statuses.items():
        check(
            f"{backend} probe returns a verdict without raising",
            isinstance(status.reachable, bool),
            (
                f"reachable={status.reachable} version={status.version or '-'} "
                f"transport={status.extra.get('transport', 'n/a')}"
            ),
        )

    reachable = [b for b, s in statuses.items() if s.reachable]
    check(
        "at least one container engine is reachable",
        bool(reachable),
        f"reachable: {', '.join(reachable) or 'NONE — see the fix lines above'}",
    )

    # --- 6. the Windows-specific routing is correct ------------------
    dialect = engine.client_dialect("podman")
    if WINDOWS:
        check(
            "Podman is routed through the Docker-compatible client",
            dialect == "docker"
            and engine.session_kwargs("podman").get("session_backend") == "docker"
            if "podman" in reachable
            else dialect == "docker",
            "podman-py has no named-pipe transport, so Windows uses "
            "Podman's Docker-compatible API",
        )
    else:
        check(
            "Podman uses its own client off Windows",
            dialect == "podman" and not engine.session_kwargs("podman"),
            f"client_dialect('podman') = {dialect}",
        )

    # --- 6b. the engine reported is the engine running ---------------
    #
    # Podman serves a Docker-compatible endpoint, so "something answered
    # the docker pipe" says nothing about what is running. A machine with
    # Podman and no Docker used to be told "docker engine reachable".
    for backend, status in statuses.items():
        if not status.reachable:
            continue
        product = status.extra.get("product")
        check(
            f"{backend} probe names the engine actually running",
            product in ("docker", "podman"),
            f"asked for {backend}, answered by {product}"
            + ("" if product == backend else "  <- endpoint is shared"),
        )

    # The selection logic under a shared endpoint, exercised without
    # needing a machine where that is true. This is the case that made
    # backend="podman" fail while backend="auto" worked.
    real_identify = engine.identify
    try:
        engine.identify = lambda _client: "podman"
        engine.reset_clients()
        resolved = engine.detect("auto")
        check(
            "auto reports the real engine when one serves another's endpoint",
            resolved == "podman",
            f"auto resolved to {resolved!r} with every endpoint served by podman",
        )
        refused = False
        try:
            engine.detect("docker")
        except engine.EngineUnavailableError as exc:
            refused = "podman" in str(exc)
        check(
            "asking for the wrong engine is refused, not silently honoured",
            refused,
            "requesting docker on a podman-only machine names podman in the error",
        )
    finally:
        engine.identify = real_identify
        engine.reset_clients()

    # --- 6c. a dropped connection is not an outage -------------------
    stale_cases = {
        "The pipe is being closed": True,
        "[WinError 232] The pipe is being closed": True,
        "[WinError 109] The pipe has been ended": True,
        "('Connection aborted.', RemoteDisconnected('Remote end closed'))": True,
        "Error while fetching server API version: FileNotFoundError(2)": False,
        "404 Client Error: Not Found": False,
    }
    wrong = [
        text
        for text, expected in stale_cases.items()
        if engine.is_stale_connection(Exception(text)) is not expected
    ]
    check(
        "dropped connections are retried and real outages are not",
        not wrong,
        "; ".join(wrong) or f"{len(stale_cases)} cases classified correctly",
    )

    # --- 6d. the generated client config is valid and complete -------
    #
    # This is the file a colleague pastes into an editor. Its failure
    # mode is silent — a bad path or a stray backslash produces "no tools
    # appeared" with no error — so it is generated rather than typed, and
    # the generation is checked here.
    from hyperbox_mcp import clientconfig

    ok = True
    detail = []
    for fmt, key in (("json", "mcpServers"), ("cursor", "servers")):
        text = clientconfig.render(fmt)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            ok = False
            detail.append(f"{fmt}: {exc}")
            continue
        entry = (parsed.get(key) or {}).get("hyperbox") or {}
        if not entry.get("command") or entry.get("args") != []:
            ok = False
            detail.append(f"{fmt}: bad entry {entry}")
        if not (entry.get("env") or {}).get("PATH"):
            ok = False
            detail.append(f"{fmt}: no PATH")
    check(
        "generated client configs are valid JSON with a launchable command",
        ok,
        "; ".join(detail) or "json and cursor formats both parse",
    )

    yaml_text = clientconfig.render("yaml")
    check(
        "the yaml config has the shape Codeaira expects",
        yaml_text.startswith("mcpServers:") and "- name: hyperbox" in yaml_text,
        yaml_text.splitlines()[1] if "\n" in yaml_text else yaml_text,
    )

    # The escaping this exists for. A Windows path carries backslashes and
    # a space; emitted naively it is not parseable at all, which is the
    # error colleagues hit by hand.
    win = r"C:\Users\Sanjay Jat\.local\bin\hyperbox.exe"
    check(
        "Windows paths survive a round trip through both serialisers",
        json.loads(json.dumps(win)) == win
        and json.loads(clientconfig._yaml_scalar(win)) == win,
        "backslashes and spaces escape correctly for JSON and YAML",
    )

    # --- 7. no POSIX-only calls left on an import path ---------------
    offenders = []
    for path in Path("src/hyperbox_mcp").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("import fcntl", "os.getuid(", "import pwd", "import termios"):
            if needle in text and "WINDOWS" not in text and "filelock" not in path.name:
                offenders.append(f"{path.name}: {needle}")
    check(
        "no unguarded POSIX-only calls in the package",
        not offenders,
        "; ".join(offenders) or "checked fcntl, getuid, pwd, termios",
    )

    return summarize()


def summarize() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("\nThis machine is NOT ready. Fix the FAIL lines, then run:")
        print("  hyperbox doctor")
        print("  python tests/run_all.py docker")
    else:
        print("\nHost side is portable here. Next: `hyperbox doctor`, then")
        print("`python tests/run_all.py docker` (and `podman`).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
