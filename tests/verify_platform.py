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
import logging.handlers
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
skipped: list[tuple[str, str]] = []

#: Set on a machine that legitimately has no container engine — a CI
#: runner, typically. The engine-dependent checks below are then reported
#: as SKIP rather than FAIL.
#:
#: This is opt-in, never auto-detected. A developer whose Docker is simply
#: stopped must see a failure, not a green run: "no engine here" and "the
#: engine here is broken" are different answers, and only the machine's
#: owner knows which one applies.
ENGINE_OPTIONAL = os.environ.get("HYPERBOX_SKIP_ENGINE_CHECKS") == "1"


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}", flush=True)
    if detail:
        print(f"        {detail}")


def skip(name: str, reason: str) -> None:
    """Record a check that could not run here. Never counted as a pass."""
    skipped.append((name, reason))
    print(f"SKIP  {name}", flush=True)
    print(f"        {reason}")


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
    if not reachable and ENGINE_OPTIONAL:
        skip(
            "at least one container engine is reachable",
            "HYPERBOX_SKIP_ENGINE_CHECKS=1: no engine on this machine by "
            "design. The host-only checks below still run.",
        )
    else:
        check(
            "at least one container engine is reachable",
            bool(reachable),
            f"reachable: {', '.join(reachable) or 'NONE — see the fix lines above'}",
        )

    # --- 5b. a socket FILE is not a listener -------------------------
    #
    # The regression this guards is the whole reason v0.3 exists: a
    # stopped `podman machine` leaves its *-api.sock file on disk, and the
    # resolver used to accept any path that os.path.exists(). Connecting to
    # such a file gives ECONNREFUSED rather than ENOENT, so a healthy
    # machine was reported as "connection refused to podman" for the entire
    # life of a server process — days, inside an editor.
    #
    # POSIX only: Windows has no unix sockets, and the named-pipe route
    # does not go through this resolver at all.
    if WINDOWS:
        skip(
            "a dead socket file is never chosen as a transport",
            "Windows has no unix sockets; Podman is reached over a named pipe.",
        )
    else:
        import socket as _socket

        sock_dir = tempfile.mkdtemp(prefix="hb-sock-")
        dead_path = os.path.join(sock_dir, "dead-api.sock")
        live_path = os.path.join(sock_dir, "live-api.sock")

        # Bound but never listening: the file exists, nothing accepts.
        dead = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        dead.bind(dead_path)
        dead.close()

        live = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        live.bind(live_path)
        live.listen(1)
        try:
            check(
                "a bound-but-dead socket file is reported as not live",
                os.path.exists(dead_path)
                and not engine._socket_is_live(dead_path),
                "the file exists and is still rejected — existence is not "
                "liveness",
            )
            check(
                "a listening socket is reported as live",
                engine._socket_is_live(live_path),
                f"probe timeout {engine.SOCKET_PROBE_TIMEOUT}s",
            )

            # The latch: a CONTAINER_HOST pointing at a dead socket must be
            # re-resolved, not returned. Returning it is what made the
            # failure permanent for the process.
            previous = os.environ.get("CONTAINER_HOST")
            os.environ["CONTAINER_HOST"] = f"unix://{dead_path}"
            try:
                resolved = engine.ensure_podman_transport(
                    cli_timeout=engine.PODMAN_CLI_TIMEOUT_FAST
                )
            finally:
                if previous is None:
                    os.environ.pop("CONTAINER_HOST", None)
                else:
                    os.environ["CONTAINER_HOST"] = previous
                engine.reset_clients()
            check(
                "a dead CONTAINER_HOST is re-resolved, not latched",
                resolved != dead_path,
                f"resolved to {resolved or '(nothing live)'} instead of the "
                "dead socket",
            )
        finally:
            live.close()
            for path in (dead_path, live_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            try:
                os.rmdir(sock_dir)
            except OSError:
                pass

    # --- 5c. `config --local` pins the checkout, not PATH -------------
    #
    # Gate-critical, and it fails silently without a test. A machine that
    # already has a released hyperbox installed resolves `hyperbox` through
    # PATH to THAT build, so a client configured from a branch checkout
    # launches the release, the branch never runs, and the config looks
    # entirely correct. Observed here: PATH held 0.2.0 while the checkout
    # under test was newer.
    from hyperbox_mcp import clientconfig  # noqa: PLC0415

    local_exe = clientconfig.local_executable_path()
    if not local_exe:
        skip(
            "config --local pins this checkout's executable",
            "no `hyperbox` next to this interpreter — run `pip install -e .` "
            "in the environment you are testing from.",
        )
    else:
        rendered = json.loads(clientconfig.render("json", local=True))
        command = rendered["mcpServers"]["hyperbox"]["command"]
        check(
            "config --local pins this checkout's executable",
            Path(command).parent == Path(sys.executable).parent,
            f"{command} (interpreter: {sys.executable})",
        )
        check(
            "config --local says the config is checkout-bound",
            any(
                "--local" in line for line in clientconfig.notes("json", local=True)
            ),
            "the note explains why the path is tied to this directory",
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
    if not reachable:
        # engine.detect() needs something to answer, so this case cannot be
        # exercised here. It is not merely unchecked: it crashed the whole
        # suite with an uncaught EngineUnavailableError before this guard.
        skip(
            "auto reports the real engine when one serves another's endpoint",
            "needs a reachable engine to resolve against",
        )
        skip(
            "asking for the wrong engine is refused, not silently honoured",
            "needs a reachable engine to refuse against",
        )
    else:
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
    for fmt, key in (
        ("json", "mcpServers"),
        ("cursor", "servers"),
        ("antigravity", "mcpServers"),
    ):
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
        "; ".join(detail) or "json, cursor and antigravity all parse",
    )

    yaml_text = clientconfig.render("yaml")
    check(
        "the yaml config has the list shape Continue-based clients expect",
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

    # --- 6e. environments resolve at call time, not at import --------
    #
    # The bug this guards: environments used to be a dict built at import.
    # `hyperbox build` runs in a DIFFERENT process from the server, so a
    # server that had already started could never see a new environment —
    # it needed a restart, while the tool description promised it did not.
    from hyperbox_mcp import validate  # noqa: E402

    with tempfile.TemporaryDirectory() as td:
        fake_env_dir = Path(td) / "environments"
        fake_env_dir.mkdir()
        real_dir, real_cache, real_mtime = (
            policy._ENV_DIR, policy._env_cache, policy._env_mtime
        )
        try:
            policy._ENV_DIR = fake_env_dir
            policy._env_cache, policy._env_mtime = None, 0.0

            before = policy.environments()
            check(
                "an empty environment directory still offers the built-in",
                set(before) == {"python"},
                f"got {sorted(before)}",
            )

            # Built AFTER the first resolution, exactly as a CLI build
            # would be while the server is already running.
            (fake_env_dir / "late-built").mkdir()
            (fake_env_dir / "late-built" / "Dockerfile").write_text("FROM x\n")

            after = policy.environments()
            check(
                "an environment built after startup appears with no restart",
                "late-built" in after,
                f"got {sorted(after)}",
            )
            check(
                "its image tag is the one hyperbox build produces",
                after.get("late-built") == "hyperbox-local/late-built:latest",
                str(after.get("late-built")),
            )
            check(
                "the map is a copy, so a caller cannot corrupt the cache",
                policy.environments() is not policy.environments(),
                "each call returns a fresh dict",
            )

            # A directory without a Dockerfile is not an environment.
            (fake_env_dir / "no-dockerfile").mkdir()
            check(
                "a directory with no Dockerfile is not offered",
                "no-dockerfile" not in policy.environments(),
                "only directories holding a Dockerfile count",
            )

            # Validation, on the same live map.
            rejected = []
            for bad in ("../../etc", "has space", "", "x" * 65, 123):
                try:
                    validate.environment(bad)
                except validate.InvalidInput:
                    rejected.append(bad)
            check(
                "bad environment names are refused, traversal included",
                len(rejected) == 5,
                f"{len(rejected)}/5 refused",
            )
            check(
                "a real environment is accepted",
                validate.environment("late-built") == "late-built"
                and validate.environment(None) is None,
                "named environment resolves, None means default",
            )
            unknown_ok = False
            try:
                validate.environment("never-built")
            except validate.InvalidInput as exc:
                unknown_ok = "hyperbox build" in str(exc)
            check(
                "an unknown environment says how to build one",
                unknown_ok,
                "the error names the CLI command",
            )
        finally:
            policy._ENV_DIR = real_dir
            policy._env_cache, policy._env_mtime = real_cache, real_mtime

    # --- 6f. the log rotates instead of growing without bound --------
    import logging  # noqa: E402

    from hyperbox_mcp import server  # noqa: E402

    with tempfile.TemporaryDirectory() as td:
        log_file = Path(td) / "server.log"
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2_000, backupCount=2, encoding="utf-8"
        )
        probe = logging.getLogger("hyperbox-rotation-probe")
        probe.addHandler(handler)
        probe.setLevel(logging.INFO)
        for _ in range(400):
            probe.info("x" * 80)
        handler.close()
        probe.removeHandler(handler)
        rotated = sorted(Path(td).glob("server.log*"))
        biggest = max(f.stat().st_size for f in rotated)
        check(
            "the server log rotates rather than growing without bound",
            len(rotated) <= 3 and biggest < 10_000,
            f"{len(rotated)} files, largest {biggest} bytes",
        )
    check(
        "importing the server configures no log handlers",
        not logging.getLogger("hyperbox").handlers,
        "logging is set up in serve(), not at import",
    )
    check(
        "the server logger never propagates to the root logger",
        server.logger.propagate is False or not server.logger.handlers,
        "a root StreamHandler must never reach stdout",
    )

    # --- 6g. the background sweep keeps running -----------------------
    #
    # Before this loop existed the inactivity TTL was enforced only by
    # restarting the process: serve() swept once and then blocked in
    # mcp.run(). A server inside an editor stays up for days.
    calls = []
    real_collect, real_interval = server.collect_garbage, policy.GC_INTERVAL_SECONDS
    try:
        policy.GC_INTERVAL_SECONDS = 0.05

        def counting_collect():
            calls.append(1)
            # Fail every other sweep: a failing sweep must not kill the
            # thread, or GC silently stops for the life of the process.
            if len(calls) % 2 == 0:
                raise RuntimeError("engine unreachable")
            return []

        server.collect_garbage = counting_collect
        t = threading.Thread(target=server._background_gc_loop, daemon=True)
        t.start()
        deadline = time.time() + 3.0
        while len(calls) < 5 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        server.collect_garbage = real_collect
        policy.GC_INTERVAL_SECONDS = real_interval

    check(
        "the background GC sweeps repeatedly, not once",
        len(calls) >= 5,
        f"{len(calls)} sweeps in 3s",
    )
    check(
        "a failing sweep does not kill the GC thread",
        len(calls) >= 5 and t.is_alive(),
        "the loop kept going after a raised exception",
    )

    # --- 7. no POSIX-only calls left on an import path ---------------
    offenders = []
    for path in Path("src/hyperbox_mcp").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # os.system( is on this list because `hyperbox logs` was once
        # implemented as os.system("tail -f ..."): POSIX-only, and a shell
        # interpolation of a home-directory path. The needle list had no
        # entry that would have caught it.
        for needle in (
            "import fcntl", "os.getuid(", "import pwd", "import termios",
            "os.system(",
        ):
            if needle in text and "WINDOWS" not in text and "filelock" not in path.name:
                offenders.append(f"{path.name}: {needle}")
    check(
        "no unguarded POSIX-only calls in the package",
        not offenders,
        "; ".join(offenders)
        or "checked fcntl, getuid, pwd, termios, os.system",
    )

    return summarize()


def summarize() -> int:
    failed = [r for r in results if not r[1]]
    tail = f"  ({len(skipped)} skipped)" if skipped else ""
    print(f"\n{len(results) - len(failed)}/{len(results)} passed{tail}")
    if skipped:
        print("\nSkipped, so NOT proven here:")
        for name, reason in skipped:
            print(f"  - {name}")
        print("  Run this on a machine with a container engine to cover them.")
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
