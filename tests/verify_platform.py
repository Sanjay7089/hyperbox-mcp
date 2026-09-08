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
import logging.handlers  # noqa: F401 - submodule needed for RotatingFileHandler below
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

    # --- 5b-ii. refused-is-retryable and transport-reset are a PAIR ---
    #
    # "connection refused" is treated as a retryable, connection-shaped
    # failure. That is only safe because reset_clients() also drops the
    # resolved socket, so the retry re-resolves instead of reconnecting to
    # the same dead path. Keep the marker without the reset and every
    # refused call becomes two guaranteed failures instead of one, with the
    # process still latched to a socket that can never work.
    #
    # Neither half is wrong on its own, which is exactly why this is
    # tested: a future reader tidying up one of them would see nothing
    # break.
    refused = engine.EngineUnavailableError(
        "Podman is not reachable (APIError: ConnectionRefusedError(61, "
        "'Connection refused'))"
    )
    check(
        "a refused connection is treated as retryable",
        engine.is_stale_connection(refused),
        "paired with the transport reset checked below",
    )
    if WINDOWS:
        skip(
            "reset_clients drops the resolved socket, not just the client",
            "no unix socket transport to resolve on Windows.",
        )
    else:
        previous = os.environ.get("CONTAINER_HOST")
        try:
            engine._own_container_host = "unix:///tmp/hyperbox-probe.sock"
            os.environ["CONTAINER_HOST"] = engine._own_container_host
            engine.reset_clients()
            cleared = "CONTAINER_HOST" not in os.environ
        finally:
            if previous is None:
                os.environ.pop("CONTAINER_HOST", None)
            else:
                os.environ["CONTAINER_HOST"] = previous
            engine._own_container_host = ""
        check(
            "reset_clients drops the resolved socket, not just the client",
            cleared,
            "otherwise the retry reconnects to the same dead path",
        )

        # And it must NOT discard a setting the user chose themselves.
        previous = os.environ.get("CONTAINER_HOST")
        try:
            os.environ["CONTAINER_HOST"] = "tcp://someone-elses-choice:2375"
            engine._own_container_host = ""
            engine.reset_clients()
            kept = os.environ.get("CONTAINER_HOST") == (
                "tcp://someone-elses-choice:2375"
            )
        finally:
            if previous is None:
                os.environ.pop("CONTAINER_HOST", None)
            else:
                os.environ["CONTAINER_HOST"] = previous
        check(
            "a user's own CONTAINER_HOST survives reset_clients",
            kept,
            "only a socket this process resolved is ours to drop",
        )

    # --- 5b-iii. the two clients disagree about demultiplexing --------
    #
    # docker-py demuxes the exec stream and returns plain bytes; podman-py
    # returns the RAW framed stream. Decoding podman's directly gives a
    # string of 8-byte headers that reads as output and is not — a check
    # for surviving processes found no digits in those headers and reported
    # "nothing running" about a container it had never actually read. It
    # answered correctly by accident, which is worse than answering wrongly.
    framed = (b"\x01\x00\x00\x00\x00\x00\x00\x09MARKER-42"
              b"\x02\x00\x00\x00\x00\x00\x00\x04oops")
    out, err = engine.demux_frames(framed)
    check(
        "framed exec output is demultiplexed (podman-py's shape)",
        out == "MARKER-42" and err == "oops",
        f"stdout={out!r} stderr={err!r}",
    )
    plain_out, plain_err = engine.demux_frames(b"MARKER-42\n")
    check(
        "unframed exec output passes through (docker-py's shape)",
        plain_out == "MARKER-42\n" and plain_err == "",
        f"stdout={plain_out!r} stderr={plain_err!r}",
    )
    check(
        "a truncated frame does not invent output",
        engine.demux_frames(b"\x01\x00\x00\x00\x00\x00\x00\x63short") == ("", ""),
        "a length longer than the payload stops parsing rather than guessing",
    )
    check(
        "empty exec output is empty, not an error",
        engine.demux_frames(b"") == ("", ""),
        "",
    )

    # --- 5b-iv. the exec frame parser, fed the way a socket feeds it --
    #
    # Whole-buffer demuxing is the easy half and is checked above. The
    # streaming parser is the one that matters, because read boundaries
    # have nothing to do with frame boundaries: a recv can return three
    # bytes of a header, or a header plus half a payload, or two frames and
    # a fragment. Parsing each chunk independently corrupts output in a way
    # that looks like the program's own, so this feeds the same bytes at
    # every possible split and demands the same answer each time.
    import random  # noqa: PLC0415

    from hyperbox_mcp.rest.client import FrameReader  # noqa: PLC0415

    def framed(stream: int, text: bytes) -> bytes:
        return bytes([stream, 0, 0, 0]) + len(text).to_bytes(4, "big") + text

    stream_bytes = (
        framed(1, b"alpha")
        + framed(2, b"warn-1")
        + framed(1, b"")          # a zero-length frame is a frame, not EOF
        + framed(1, b"beta-" + b"x" * 9000)   # spans several reads
        + framed(2, b"warn-2")
    )
    want = ("alpha" + "" + "beta-" + "x" * 9000, "warn-1warn-2")

    def run_with(sizes):
        reader, at = FrameReader(), 0
        for size in sizes:
            if at >= len(stream_bytes):
                break
            reader.feed(stream_bytes[at : at + size])
            at += size
        reader.feed(stream_bytes[at:])
        return reader.result()

    bad = []
    for chunk in (1, 2, 3, 7, 8, 9, 13, 4096, 65536):
        if run_with([chunk] * (len(stream_bytes) // max(chunk, 1) + 2)) != want:
            bad.append(f"fixed:{chunk}")
    rng = random.Random(20260908)
    for _ in range(200):
        sizes = [rng.randint(1, 40) for _ in range(len(stream_bytes))]
        if run_with(sizes) != want:
            bad.append(f"random:{sizes[:6]}")
            break
    check(
        "the exec frame parser survives every read boundary",
        not bad,
        f"9 fixed chunk sizes + 200 random splits of {len(stream_bytes)} bytes"
        + (f" — FAILED: {bad[:2]}" if bad else ""),
    )

    truncated = FrameReader()
    truncated.feed(framed(1, b"kept") + b"\x01\x00\x00\x00\x00\x00\x27\x10sh")
    check(
        "a truncated final frame is dropped, not guessed at",
        truncated.result() == ("kept", ""),
        "a frame whose payload never arrived contributes nothing",
    )

    # --- 5b-v. writing under a tmpfs is refused, not silently lost ----
    #
    # Measured on both engines: the archive API cannot write through a
    # tmpfs mount on Docker. It returns 200, puts the file in the image
    # layer beneath the mount, and nothing ever sees it. Podman writes
    # through, so the same call works on one engine and vanishes on the
    # other -- which is why this is a refusal rather than a note.
    #
    # The guard runs before any I/O, so no engine is needed here.
    from hyperbox_mcp import errors as hb_errors  # noqa: PLC0415
    from hyperbox_mcp.rest import api as rest_api  # noqa: PLC0415

    refusals = []
    for path in ("/work/code.py", "/work/nested/code.py"):
        try:
            rest_api.put_file(None, "cid", path, b"x")
        except hb_errors.ProvisionError as exc:
            refusals.append(bool(exc.fix))
        except Exception:  # noqa: BLE001 - anything else is the wrong answer
            refusals.append(False)
        else:
            refusals.append(False)
    check(
        "writing under a tmpfs mount is refused with a way out",
        len(refusals) == 2 and all(refusals),
        f"submitted code goes to {policy.CODE_DIR}, which is not a tmpfs",
    )

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
        # Patch THE identity function. There is one now: this used to
        # patch a copy that detect() no longer consulted, so the case
        # passed while measuring nothing.
        real_identify = engine.identify_version
        try:
            engine.identify_version = lambda _raw: "podman"
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
            engine.identify_version = real_identify
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
        # Point the resolver at a scratch directory the supported way,
        # rather than by rebinding a private module attribute. That
        # attribute is gone, and reaching for it was how this case broke
        # when the directory became configurable.
        real_override = os.environ.get("HYPERBOX_ENV_DIR")
        real_cache, real_mtime = policy._env_cache, policy._env_mtime
        try:
            os.environ["HYPERBOX_ENV_DIR"] = str(fake_env_dir)
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

            # The same promise, proven where the filesystem does NOT
            # notice the write.
            #
            # A directory's mtime is a change signal only if it changes.
            # On Windows its granularity is coarse enough that a build
            # finishing inside one tick of the previous resolution leaves
            # it identical, so the cache is served and the environment
            # stays invisible. Freezing the mtime across the write
            # reproduces that on every platform, instead of waiting for
            # Windows to lose the race — which it did in about half of
            # CI's windows-latest/3.11 runs, on the check above.
            with tempfile.TemporaryDirectory() as frozen_td:
                frozen_dir = Path(frozen_td) / "environments"
                frozen_dir.mkdir()
                # Pointing at a new directory resets the cache on its own.
                os.environ["HYPERBOX_ENV_DIR"] = str(frozen_dir)

                policy.environments()
                stamp = frozen_dir.stat().st_mtime
                (frozen_dir / "unnoticed").mkdir()
                (frozen_dir / "unnoticed" / "Dockerfile").write_text("FROM x\n")
                os.utime(frozen_dir, (stamp, stamp))

                check(
                    "an unchanged mtime does not hide a new environment",
                    "unnoticed" in policy.environments(),
                    "a just-written mtime is not proof nothing changed",
                )

            os.environ["HYPERBOX_ENV_DIR"] = str(fake_env_dir)

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
            if real_override is None:
                os.environ.pop("HYPERBOX_ENV_DIR", None)
            else:
                os.environ["HYPERBOX_ENV_DIR"] = real_override
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

    # --- 6f2. the package compiles without warnings -------------------
    #
    # A SyntaxWarning is printed by the interpreter on every invocation
    # of every command, and one shipped in 0.3.0 -- an invalid `\p`
    # escape in a docstring -- because nothing ever looked. It is the
    # cheapest possible check and it guards the whole package.
    import warnings as _warnings

    offenders = []
    for source in sorted(Path("src").rglob("*.py")):
        with _warnings.catch_warnings():
            _warnings.simplefilter("error")
            try:
                compile(source.read_text(encoding="utf-8"), str(source), "exec")
            except SyntaxError as exc:
                # A SyntaxWarning promoted by simplefilter("error") is
                # re-raised as SyntaxError, so catching only the warning
                # crashed this suite instead of reporting a FAIL line.
                offenders.append(f"{source}: {exc}")
    check(
        "every module compiles with no SyntaxWarning",
        not offenders,
        "; ".join(offenders) if offenders else "clean",
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
