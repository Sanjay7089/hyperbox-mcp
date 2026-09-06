# Setup

## Requirements

- **Python 3.11+**
- **Docker or Podman**, running. Either works; HyperBox picks whichever
  it finds.

## Install

```bash
pip install hyperbox-mcp
```

Or, to get an isolated interpreter and a stable executable path — which
is what an MCP client needs:

```bash
uv tool install hyperbox-mcp      # or: pipx install hyperbox-mcp
```

That puts one executable on your `PATH`:

| Platform | Path |
|---|---|
| macOS / Linux | `~/.local/bin/hyperbox` |
| Windows | `%USERPROFILE%\.local\bin\hyperbox.exe` |

## Check the machine

```bash
hyperbox doctor
```

This checks Python, both container engines, which one `auto` resolves to,
the sandbox image, the registry, and then does a real create → run →
destroy round trip. It exits non-zero if anything fails.

Pull the sandbox image once before first use, so a multi-gigabyte
download never happens inside a client request:

```bash
hyperbox doctor --pull
```

## Connect it to your client

Do not hand-write the config. Generate it:

```bash
hyperbox config --format json          # Claude Desktop, and most clients
hyperbox config --format cursor        # Cursor / VS Code
hyperbox config --format antigravity   # Antigravity
hyperbox config --format yaml          # Codeaira
```

The config goes to stdout and the guidance to stderr, so you can redirect
it cleanly. Merge it into the file your client uses:

| Client | File | Key |
|---|---|---|
| Claude Desktop | `claude_desktop_config.json` | `mcpServers` |
| Cursor / VS Code | `.vscode/mcp.json` | `servers` |
| Antigravity | `~/.gemini/antigravity/mcp_config.json` | `mcpServers` |
| Codeaira | `mcpservers/config.yaml` | `mcpServers` |

Then **restart the client** — MCP configs are read at launch.

### Why generate it

Every value in the config is something HyperBox already knows: where its
own executable is, and where your container CLI is. The failure mode of
getting it wrong is silent — no tools appear, with no error anywhere — so
it is worth not typing by hand. On Windows especially, paths carry
backslashes and spaces that must be escaped correctly.

Two things the generator gets right that hand-editing often does not:

- **`args` is empty.** A config of the form
  `["run", "--project", "…", "hyperbox"]` launches through a source
  checkout, and breaks the moment that folder moves or changes branch.
- **`PATH` includes your container CLI's directory.** Clients launch
  servers with a trimmed environment, so Docker or Podman is frequently
  invisible otherwise.

## Environments

Every sandbox starts from a base image. The default is a plain Python
image, so anything beyond the standard library is a package install on
each new sandbox.

To start from something heavier, build an environment once:

```bash
cat > Dockerfile <<'DOCKERFILE'
FROM ghcr.io/vndee/sandbox-python-311-bullseye
RUN pip install numpy pandas
DOCKERFILE

hyperbox build data-science --custom ./Dockerfile
hyperbox envs
```

Your agent can then ask for it by name:

```
create_sandbox(environment="data-science")
```

A running server picks up a new environment without a restart.

**Building is a CLI action, deliberately.** A build runs whatever the
Dockerfile says — arbitrary commands, as root, with network access, under
none of the limits that apply to a sandbox. There is no MCP tool that
builds an environment, so an agent can use what you made and cannot make
one. See [security.md](security.md).

## Where things live

| Directory | Holds |
|---|---|
| `~/.hyperbox/state/` | the sandbox registry and lock files |
| `~/.hyperbox/logs/` | `server.log`, rotated at 5 MB, three kept |
| `~/.hyperbox/environments/` | one directory per custom environment |

Override the state directory with `HYPERBOX_STATE_DIR`. Set
`HYPERBOX_TTL_SECONDS` to change how long an idle sandbox survives
(default 1800).

## Command reference

```
hyperbox                 Start the MCP server on stdio (default)
hyperbox doctor          Check this machine can run sandboxes
  --pull                 Also pull the sandbox image if missing
  --quick                Skip the live create/run/destroy check
hyperbox config          Print a ready-to-paste MCP client config
  --format json|cursor|antigravity|yaml
hyperbox envs            List environments create_sandbox can use
hyperbox build <name>    Build an environment from a Dockerfile
  --custom <path>        Copy that Dockerfile in and build it
hyperbox logs            Show the server log
  --follow               Keep printing as new lines arrive
hyperbox --version       Print the installed version
```

Something not working? See [troubleshooting.md](troubleshooting.md).
