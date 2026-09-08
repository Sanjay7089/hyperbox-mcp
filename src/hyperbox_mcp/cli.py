"""Terminal subcommands for the `hyperbox` executable.

Kept apart from server.py so that starting the MCP server imports none of
it. Anything here may print to stdout; the server may not, because a
stdio MCP client is reading that stream.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version


def usage() -> str:
    return (
        "Usage:\n"
        "  hyperbox                 Start the MCP server on stdio (default)\n"
        "  hyperbox doctor          Check this machine can run sandboxes\n"
        "    --pull                 Also pull the sandbox image if missing\n"
        "    --quick                Skip the live create/run/destroy check\n"
        "  hyperbox config          Print a ready-to-paste MCP client config\n"
        "    --format json          mcpServers block (Claude Desktop, generic)\n"
        "    --format cursor        servers block (.vscode/mcp.json)\n"
        "    --format yaml          YAML list (Continue-based clients)\n"
        "    --format antigravity   Antigravity mcp_config.json\n"
        "    --local                Pin this checkout's executable, not PATH\n"
        "  hyperbox envs            List environments create_sandbox can use\n"
        "  hyperbox build <name>    Create an environment agents can select\n"
        "    --dockerfile <path>    Build it, from a file or a directory\n"
        "    --image <ref>          Pull an existing image and register it\n"
        "    --engine docker|podman Override which engine to use\n"
        "    --no-cache             Build without reusing cached layers\n"
        "  hyperbox logs            Show the server log\n"
        "    --follow               Keep printing as new lines arrive\n"
        "  hyperbox --version       Print the installed version\n"
    )


def _version() -> str:
    try:
        return version("hyperbox-mcp")
    except PackageNotFoundError:  # pragma: no cover - running from source
        return "unknown (not installed as a package)"


def _logs(follow: bool = False) -> int:
    """Print the server log, optionally following it.

    Written in Python rather than shelling out to `tail -f`: this package
    is expected to work on Windows, where there is no tail, and building
    a shell command out of a home-directory path invites quoting bugs.
    """
    import time

    from hyperbox_mcp.server import LOG_FILE

    if not LOG_FILE.exists():
        print(f"No log yet at {LOG_FILE}")
        print("It is created when the server next starts.")
        return 1

    with LOG_FILE.open("r", encoding="utf-8", errors="replace") as fh:
        sys.stdout.write(fh.read())
        if not follow:
            return 0
        sys.stdout.flush()
        try:
            while True:
                line = fh.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.4)
        except KeyboardInterrupt:
            return 0


def dispatch(argv: list[str]) -> int:
    command, *rest = argv

    if command in {"--version", "-V"}:
        print(f"hyperbox {_version()}")
        return 0

    if command in {"help", "--help", "-h"}:
        print(usage())
        return 0

    if command == "doctor":
        unknown = [a for a in rest if a not in {"--pull", "--quick"}]
        if unknown:
            print(f"hyperbox doctor: unknown option {unknown[0]!r}\n")
            print(usage())
            return 2
        from hyperbox_mcp.doctor import run_doctor

        return run_doctor(pull="--pull" in rest, live="--quick" not in rest)

    if command == "config":
        fmt = "json"
        local = False
        rest_iter = list(rest)
        while rest_iter:
            arg = rest_iter.pop(0)
            if arg == "--format":
                if not rest_iter:
                    print("hyperbox config: --format needs a value\n")
                    print(usage())
                    return 2
                fmt = rest_iter.pop(0)
            elif arg.startswith("--format="):
                fmt = arg.split("=", 1)[1]
            elif arg == "--local":
                local = True
            else:
                print(f"hyperbox config: unknown option {arg!r}\n")
                print(usage())
                return 2
        if fmt not in {"json", "cursor", "yaml", "antigravity"}:
            print(f"hyperbox config: unknown format {fmt!r}. "
                  "Use json, cursor, yaml or antigravity.\n")
            return 2
        from hyperbox_mcp.clientconfig import print_config

        return print_config(fmt, local=local)

    if command == "logs":
        unknown = [a for a in rest if a not in {"--follow", "-f"}]
        if unknown:
            print(f"hyperbox logs: unknown option {unknown[0]!r}\n")
            print(usage())
            return 2
        return _logs(follow=bool(rest))

    if command == "envs":
        if rest:
            print(f"hyperbox envs: unexpected argument {rest[0]!r}\n")
            print(usage())
            return 2
        from hyperbox_mcp.builder import list_environments

        return list_environments()

    if command == "build":
        if not rest:
            print("hyperbox build: missing environment name\n")
            print(usage())
            return 2
        name, *opts = rest
        dockerfile = image = None
        engine_choice, no_cache = "auto", False
        takes_value = {"--dockerfile", "--custom", "--image", "--engine"}
        while opts:
            arg = opts.pop(0)
            key, _, inline = arg.partition("=")
            if key in takes_value:
                value = inline if inline else (opts.pop(0) if opts else "")
                if not value:
                    print(f"hyperbox build: {key} needs a value\n")
                    print(usage())
                    return 2
                if key in ("--dockerfile", "--custom"):
                    dockerfile = value          # --custom is the v0.2 spelling
                elif key == "--image":
                    image = value
                else:
                    engine_choice = value
            elif arg == "--no-cache":
                no_cache = True
            else:
                print(f"hyperbox build: unknown option {arg!r}\n")
                print(usage())
                return 2
        if engine_choice not in ("auto", "docker", "podman"):
            print(f"hyperbox build: unknown engine {engine_choice!r}. "
                  "Use auto, docker or podman.\n")
            return 2
        from hyperbox_mcp.builder import run_build

        return run_build(name, dockerfile, image, engine_choice, no_cache)

    print(usage())
    return 2
