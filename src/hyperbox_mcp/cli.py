"""Terminal subcommands for the `hyperbox` executable.

Kept apart from server.py so that starting the MCP server imports none of
it. Anything here may print to stdout; the server may not, because a
stdio MCP client is reading that stream.
"""

from __future__ import annotations

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
        "    --format yaml          Codeaira mcpservers/config.yaml\n"
        "  hyperbox envs            List environments create_sandbox can use\n"
        "  hyperbox build <name>    Build an environment from a Dockerfile\n"
        "    --custom <path>        Copy that Dockerfile in and build it\n"
        "  hyperbox --version       Print the installed version\n"
    )


def _version() -> str:
    try:
        return version("hyperbox-mcp")
    except PackageNotFoundError:  # pragma: no cover - running from source
        return "unknown (not installed as a package)"


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
            else:
                print(f"hyperbox config: unknown option {arg!r}\n")
                print(usage())
                return 2
        if fmt not in {"json", "cursor", "yaml"}:
            print(f"hyperbox config: unknown format {fmt!r}. "
                  "Use json, cursor or yaml.\n")
            return 2
        from hyperbox_mcp.clientconfig import print_config

        return print_config(fmt)

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
        custom = None
        while opts:
            arg = opts.pop(0)
            if arg == "--custom":
                if not opts:
                    print("hyperbox build: --custom needs a path\n")
                    print(usage())
                    return 2
                custom = opts.pop(0)
            elif arg.startswith("--custom="):
                custom = arg.split("=", 1)[1]
            else:
                print(f"hyperbox build: unknown option {arg!r}\n")
                print(usage())
                return 2
        from hyperbox_mcp.builder import run_build

        return run_build(name, custom)

    print(usage())
    return 2
