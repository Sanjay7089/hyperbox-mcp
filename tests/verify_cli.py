"""The `hyperbox` command line, driven as a real subprocess.

    python tests/verify_cli.py [docker|podman]

Every other suite imports the package and calls into it. This one runs
the installed executable and reads what comes out, because that is what
a person setting HyperBox up actually experiences — and because the
things that go wrong here go wrong at the edges the library never sees:
argument parsing, exit codes, which stream output lands on, and warnings
the interpreter prints before main() is even reached.

That last one is not hypothetical. 0.3.0 shipped an invalid `\p` escape
that printed a SyntaxWarning on every invocation, `hyperbox --version`
included. Nothing caught it because nothing looked at stderr.

Two checks here have "empty" as their healthy answer — no stderr, no
traceback — and a check whose pass condition is the absence of something
is decoration until its failure has been seen. So, measured rather than
assumed:

  - The stderr check catches an invalid escape only on Python 3.12+. In
    3.11 the compiler raises DeprecationWarning, which the default
    filters ignore; 3.12 promoted it to SyntaxWarning, which they show.
    That is exactly why this shipped: it was invisible in a 3.11 venv
    and loud on the 3.13 interpreter a `uv tool install` picked. The
    version-independent guard is in verify_platform.py, which compiles
    every module with warnings as errors and catches it on both.
  - The traceback check does not go red if only the CLI error boundary
    is removed, because doctor.py also catches EngineError. Two layers
    deliberately, so proving this one fails means removing both.

The stderr check still earns its place: it catches anything else that
leaks onto stderr, on every version.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

results: list[tuple[str, bool, str]] = []

#: A socket path nothing is listening on. Pointing DOCKER_HOST here makes
#: an engine genuinely unreachable, rather than simulating it.
DEAD_SOCKET = "unix:///tmp/hyperbox-no-such-engine.sock"


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}", flush=True)
    if detail:
        print(f"        {detail}")


def run(*args: str, env: dict | None = None, timeout: float = 120):
    """Invoke the CLI the way a person would, capturing both streams.

    Through `sys.executable -m` rather than a bare `hyperbox`, so the
    suite tests the checkout it was run from instead of whatever the PATH
    happens to resolve -- which on a machine with a release installed is
    a different program entirely.
    """
    full = dict(os.environ)
    full.pop("HYPERBOX_RUNTIME", None)
    if env:
        full.update(env)
    return subprocess.run(
        [sys.executable, "-m", "hyperbox_mcp.server", *args],
        capture_output=True, text=True, timeout=timeout, env=full,
        cwd=Path(__file__).resolve().parent.parent,
    )


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "docker"
    print(f"=== HyperBox CLI surface ({backend}) ===\n", flush=True)

    # --- 1. version, and the stream it does not write to ---------------
    #
    # The version itself matters less than stderr being empty. An
    # interpreter warning here is printed on EVERY command a user ever
    # runs, and is invisible to every other suite in this repo.
    r = run("--version")
    check(
        "--version prints the version and exits 0",
        r.returncode == 0 and "hyperbox" in r.stdout.lower(),
        f"exit={r.returncode} stdout={r.stdout.strip()!r}",
    )
    check(
        "--version writes NOTHING to stderr",
        r.stderr.strip() == "",
        f"stderr={r.stderr.strip()[:160]!r}" if r.stderr.strip() else "clean",
    )

    # --- 2. help and unknown input -------------------------------------
    r = run("help")
    check(
        "help prints usage and exits 0",
        r.returncode == 0 and "Usage:" in r.stdout,
        f"exit={r.returncode}",
    )
    r = run("definitely-not-a-command")
    check(
        "an unknown command exits non-zero rather than doing something",
        r.returncode != 0,
        f"exit={r.returncode}",
    )
    r = run("doctor", "--not-a-flag")
    check(
        "an unknown flag is refused with exit 2, not ignored",
        r.returncode == 2,
        f"exit={r.returncode}",
    )

    # --- 3. config: stdout is paste-clean ------------------------------
    #
    # docs/setup.md promises "the config goes to stdout and the guidance
    # to stderr, so you can redirect it cleanly". A user who redirects
    # and gets prose mixed into their JSON has a broken client config and
    # no error to explain it, so the promise is worth asserting.
    for fmt in ("json", "cursor"):
        r = run("config", "--format", fmt)
        ok = False
        detail = f"exit={r.returncode}"
        if r.returncode == 0:
            try:
                parsed = json.loads(r.stdout)
                ok = isinstance(parsed, dict) and bool(parsed)
                detail = f"top-level keys: {sorted(parsed)}"
            except json.JSONDecodeError as exc:
                detail = f"stdout is not valid JSON: {exc}"
        check(f"config --format {fmt} puts only JSON on stdout", ok, detail)

    r = run("config", "--format", "yaml")
    check(
        "config --format yaml emits a config on stdout",
        r.returncode == 0 and "hyperbox" in r.stdout,
        f"exit={r.returncode} bytes={len(r.stdout)}",
    )
    r = run("config", "--format", "nonsense")
    check(
        "an unknown config format is refused",
        r.returncode == 2,
        f"exit={r.returncode}",
    )

    # --local exists so a tester can point a client at a checkout rather
    # than at whatever `hyperbox` PATH finds -- which is how you end up
    # testing a released version and believing it was your branch.
    r = run("config", "--format", "json", "--local")
    here = str(Path(__file__).resolve().parent.parent)
    command = ""
    if r.returncode == 0:
        try:
            entry = json.loads(r.stdout)["mcpServers"]["hyperbox"]
            command = entry.get("command", "")
        except (json.JSONDecodeError, KeyError, TypeError):
            command = ""
    check(
        "config --local pins this checkout, not a PATH lookup",
        bool(command) and here in command,
        f"command={command!r}",
    )

    # --- 4. doctor, on a machine whose engine cannot be reached --------
    #
    # doctor's entire job is explaining an unhealthy machine, so the one
    # state it must handle gracefully is "no engine". It raised
    # NoEngineError straight through argv dispatch until 0.3.1: the
    # command that exists to diagnose a broken setup was the one that
    # crashed on it.
    r = run("doctor", "--quick", env={
        "DOCKER_HOST": DEAD_SOCKET,
        "CONTAINER_HOST": DEAD_SOCKET,
        "HYPERBOX_SKIP_ENGINE_CHECKS": "0",
    })
    combined = r.stdout + r.stderr
    check(
        "doctor with no reachable engine never prints a traceback",
        "Traceback (most recent call last)" not in combined,
        "traceback present" if "Traceback" in combined else "clean",
    )
    check(
        "doctor with no reachable engine exits non-zero",
        r.returncode != 0,
        f"exit={r.returncode}",
    )
    check(
        "doctor says how to start an engine rather than just failing",
        "podman machine start" in combined or "Docker Desktop" in combined,
        combined.strip().splitlines()[-1][:110] if combined.strip() else "no output",
    )

    # --- 5. envs -------------------------------------------------------
    r = run("envs")
    check(
        "envs lists the built-in python environment",
        r.returncode == 0 and "python" in r.stdout,
        f"exit={r.returncode}",
    )
    check(
        "envs names where each environment came from",
        "SOURCE" in r.stdout.upper(),
        r.stdout.strip().splitlines()[0][:110] if r.stdout.strip() else "no output",
    )

    # --- 6. build: argument handling -----------------------------------
    #
    # The build itself needs an engine and minutes; these are the checks
    # that do not, and they are where the mistakes actually are.
    r = run("build")
    check(
        "build with no name is refused, not attempted",
        r.returncode == 2,
        f"exit={r.returncode}",
    )
    r = run("build", "demo", "--dockerfile")
    check(
        "build --dockerfile with no value is refused",
        r.returncode == 2,
        f"exit={r.returncode}",
    )
    r = run("build", "../escape", "--image", "python:3.12-slim")
    check(
        "build refuses an environment name that escapes its directory",
        r.returncode != 0,
        f"exit={r.returncode}",
    )

    # --- 7. logs -------------------------------------------------------
    #
    # The first-run case: `hyperbox logs` before any server has written
    # one. HOME is redirected rather than HYPERBOX_STATE_DIR, because the
    # log path is Path.home()/".hyperbox"/"logs" with no override of its
    # own -- state and environments both have one, logs does not. Without
    # this the check reads the developer's real log and proves nothing.
    with tempfile.TemporaryDirectory() as td:
        r = run("logs", env={"HOME": td, "USERPROFILE": td})
        out = (r.stdout + r.stderr).strip()
        check(
            "logs with no log yet explains rather than crashing",
            "Traceback (most recent call last)" not in out and out != "",
            f"exit={r.returncode} out={out[:100]!r}",
        )

    return summarize()


def summarize() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("\nFAILED:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
