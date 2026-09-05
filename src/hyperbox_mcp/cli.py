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

    print(usage())
    return 2
