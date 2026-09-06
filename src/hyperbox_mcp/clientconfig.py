"""Generate a ready-to-paste MCP client configuration.

Every value in a client config is something this process already knows:
where its own executable lives, and where the container CLI lives. Asking
a person to retype those by hand is where setup actually fails — most
sharply on Windows, where an absolute path routinely contains a space and
every backslash has to be escaped for JSON, in a file whose only failure
mode is "no tools appeared" with no error anywhere.

So the escaping is done by serialising rather than by hand. `json.dumps`
gets Windows paths right by construction, and YAML's double-quoted
scalars use the same escape rules, so the YAML output quotes its strings
the same way — which is also why this needs no YAML dependency.

Notes go to stderr and the config to stdout, so `hyperbox config >
mcp.json` writes a clean file while a person still sees the guidance.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from hyperbox_mcp import engine, policy

#: The server name a client will show these tools under.
SERVER_NAME = "hyperbox"

#: Directories that must be on PATH for the server to find its engine,
#: beyond whatever the engine CLI's own directory turns out to be.
_SYSTEM_PATH_DIRS = {
    "win32": (r"C:\Windows\System32", r"C:\Windows"),
}
_POSIX_PATH_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")


def executable_path() -> str:
    """Absolute path to the `hyperbox` command a client should launch.

    `shutil.which` is preferred over `sys.argv[0]`: it resolves the name
    the way a client would, which is the thing being configured.
    """
    found = shutil.which("hyperbox")
    if found:
        return os.path.abspath(found)
    # Running as `python -m hyperbox_mcp.server`, or a console script not
    # on PATH. argv[0] still names something launchable.
    argv0 = sys.argv[0] or ""
    if argv0 and os.path.exists(argv0):
        return os.path.abspath(argv0)
    return "hyperbox"


def _looks_like_a_checkout(executable: str) -> bool:
    """Whether this executable lives inside a source tree rather than an
    installed tool — `uv run` in a clone produces exactly that."""
    parts = {p.lower() for p in Path(executable).parts}
    return bool({".venv", "venv"} & parts)


def path_entries() -> list[str]:
    """Directories a client must put on PATH, most specific first.

    Clients launch servers with a trimmed environment, so the container
    CLI is frequently unreachable unless it is named explicitly. The
    engine probe already knows where it is.
    """
    entries: list[str] = []

    def add(directory: str) -> None:
        if directory and directory not in entries:
            entries.append(directory)

    # The executable's own directory, so a client that resolves by name
    # still finds it.
    add(os.path.dirname(executable_path()))

    for backend in policy.BACKENDS:
        try:
            status = engine.probe(backend)
        except Exception:  # noqa: BLE001 - a config is useful without an engine
            continue
        if status.binary:
            add(os.path.dirname(status.binary))

    for directory in _SYSTEM_PATH_DIRS.get(sys.platform, _POSIX_PATH_DIRS):
        add(directory)
    return entries


def _server_entry() -> dict:
    separator = ";" if sys.platform == "win32" else ":"
    return {
        "command": executable_path(),
        "args": [],
        "env": {"PATH": separator.join(path_entries())},
    }


def _yaml_scalar(value: str) -> str:
    """A double-quoted YAML scalar.

    YAML's double-quoted style uses JSON's escape rules, so serialising
    with json.dumps produces a correct — and correctly escaped — scalar.
    That is the whole point: Windows backslashes are handled by the
    serialiser rather than by whoever is editing the file.
    """
    return json.dumps(value)


def render(fmt: str = "json") -> str:
    """The configuration block for `fmt`, ready to paste."""
    entry = _server_entry()

    if fmt == "yaml":
        # Continue-based clients: a YAML list under mcpServers.
        lines = [
            "mcpServers:",
            f"  - name: {SERVER_NAME}",
            f"    command: {_yaml_scalar(entry['command'])}",
            "    args: []",
            "    env:",
            f"      PATH: {_yaml_scalar(entry['env']['PATH'])}",
        ]
        return "\n".join(lines)

    # Cursor and VS Code use "servers" in .vscode/mcp.json; Claude Desktop,
    # Antigravity and most others use "mcpServers". Same object either way,
    # so antigravity needs no branch here — only a different file to put it
    # in, which notes() names.
    key = "servers" if fmt == "cursor" else "mcpServers"
    return json.dumps({key: {SERVER_NAME: entry}}, indent=2)


def notes(fmt: str) -> list[str]:
    """Guidance printed to stderr, so stdout stays paste-clean."""
    executable = executable_path()
    out = []

    if fmt == "yaml":
        out.append(
            "Continue-based clients: merge into your mcpservers YAML "
            "config (often mcpservers/config.yaml)"
        )
    elif fmt == "cursor":
        out.append("Cursor / VS Code: merge into .vscode/mcp.json")
    elif fmt == "antigravity":
        out.append(
            "Antigravity: merge into ~/.gemini/antigravity/mcp_config.json"
        )
        out.append(
            "  (restart Antigravity afterwards; it reads the file at startup)"
        )
    else:
        out.append("Merge into your client's MCP config (Claude Desktop: "
                   "claude_desktop_config.json)")

    out.append("")
    if _looks_like_a_checkout(executable):
        out.append(
            "WARNING: this command lives inside a source checkout, so the "
            "config below is tied to that folder and breaks if you move or "
            "delete it. Install it as a tool, then run this again:"
        )
        out.append("    uv tool install hyperbox-mcp")
        out.append(f"  (current: {executable})")
    else:
        out.append(
            "No checkout is needed: this launches the installed executable "
            "directly, so the repository can be moved or deleted."
        )

    reachable = [b for b in policy.BACKENDS if engine.probe(b).reachable]
    if reachable:
        out.append("")
        out.append(f"Container engine found: {', '.join(reachable)}")
    else:
        out.append("")
        out.append(
            "WARNING: no container engine is reachable right now. The "
            "config below is still correct — start Docker or Podman, then "
            "run `hyperbox doctor`."
        )

    out.append("")
    out.append(
        "Before the first sandbox, pull the image once so a multi-gigabyte "
        "download never happens inside a client request (clients time a "
        "request out long before it could finish):"
    )
    out.append("    hyperbox doctor --pull")
    return out


def print_config(fmt: str = "json") -> int:
    for line in notes(fmt):
        print(line, file=sys.stderr)
    print("", file=sys.stderr)
    print(render(fmt))
    return 0
