# Deploying HyperBox inside an organisation

HyperBox is a pure-Python package with no compiled extensions, so it
builds to a single wheel that installs identically on macOS, Linux and
Windows. Nothing here needs a package index, a container registry of your
own, or network access beyond the sandbox image.

## Installing a tagged build

The repository is private, so every route below needs GitHub access to
it. Nothing is published to a public index.

**From the repository, with git** — simplest if the machine already has
credentials for the repo:

```bash
uv tool install "git+https://github.com/Sanjay7089/hyperbox-mcp@v0.1.1"
hyperbox doctor
```

**From a release asset**, for a machine without git access:

```bash
gh release download v0.1.1 --repo Sanjay7089/hyperbox-mcp --pattern "*.whl"
uv tool install hyperbox_mcp-0.1.1-py3-none-any.whl
```

Both put a single `hyperbox` executable on the path and carry their own
interpreter. Upgrading is the same command with `--force` and a new tag.

## Build one artifact

```bash
uv build
# dist/hyperbox_mcp-0.1.0-py3-none-any.whl   (~40 KB)
# dist/hyperbox_mcp-0.1.0.tar.gz
```

`py3-none-any` means one file for every platform and architecture. That
wheel is the unit of distribution — put it on a share, in an internal
index, or attach it to a release.

## Install it so a client can launch it

The MCP client spawns the server as a subprocess, so `hyperbox` has to
resolve on `PATH`. Install it as a **tool**, not into a project
environment — that gives it its own isolated interpreter and a stable
executable path:

```bash
uv tool install hyperbox_mcp-0.1.0-py3-none-any.whl
# or, equivalently
pipx install hyperbox_mcp-0.1.0-py3-none-any.whl
```

Either way you get one executable:

| Platform | Path |
|---|---|
| macOS / Linux | `~/.local/bin/hyperbox` |
| Windows | `%USERPROFILE%\.local\bin\hyperbox.exe` |

Verify with no repository and no virtualenv anywhere near it:

```bash
hyperbox --version
hyperbox doctor
```

`doctor` exits 0 only if it created a real sandbox, ran code in it and
destroyed it.

### From an internal index

If you have one, publishing there makes upgrades ordinary:

```bash
uv tool install --index-url https://packages.internal/simple hyperbox-mcp
```

### Without installing at all

For a one-off trial, `uvx` runs the wheel without leaving anything
behind:

```bash
uvx --from ./hyperbox_mcp-0.1.0-py3-none-any.whl hyperbox doctor
```

This is fine for evaluation but a poor choice for a client's config: it
re-resolves dependencies on every launch.

## Wire it into an MCP client

Let the tool write the config rather than typing paths by hand:

```bash
hyperbox config                  # Claude Desktop and most clients
hyperbox config --format cursor  # Cursor / VS Code, .vscode/mcp.json
hyperbox config --format yaml    # Codeaira
```

It fills in its own absolute path and a `PATH` containing the container
CLI it detected, escaped correctly for the format. Guidance goes to
stderr and the config to stdout, so this writes a clean file:

```bash
hyperbox config --format cursor > .vscode/mcp.json
```

Worked examples per client are in
[client-examples/](client-examples/README.md).

**No checkout is required to run the server.** Nothing in the package
reads a repository file at runtime, so once installed as a tool the clone
can be moved or deleted. A config of the form
`uv run --project <repo> hyperbox` still works but ties every client to
that folder and re-resolves the environment on each launch; `hyperbox
config` warns when it detects it is running that way.

Any client that speaks stdio takes the same shape. Use an **absolute
path** to the executable and give it a `PATH` that includes your
container CLI — MCP clients commonly launch servers with a trimmed
environment, and a bare `hyperbox` often will not resolve.

```json
{
  "mcpServers": {
    "hyperbox": {
      "command": "/Users/you/.local/bin/hyperbox",
      "args": [],
      "env": { "PATH": "/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

On Windows:

```json
{
  "mcpServers": {
    "hyperbox": {
      "command": "C:\\Users\\you\\.local\\bin\\hyperbox.exe",
      "args": [],
      "env": { "PATH": "C:\\Program Files\\Docker\\Docker\\resources\\bin;C:\\Windows\\System32" }
    }
  }
}
```

Nothing about the server is client-specific. If your extension builds the
config programmatically, the only values it needs are the executable path
and that `PATH`.

## What each machine needs

1. **A container engine running** — Docker Desktop, or Podman with its
   machine started. Both are supported.
2. **The sandbox image, pulled during provisioning — not optional.**

   ```bash
   hyperbox doctor --pull
   ```

   The image is several gigabytes. No MCP client will wait for that
   inside a tool call: Codeaira defaults to a 30-second timeout and
   others are not much longer, so a first `create_sandbox` on a cold
   machine is cut off partway through the download. Pulling once during
   setup removes the problem for every client, whatever its timeout.

3. Nothing else. No Python on `PATH` is required by the client: the tool
   install carries its own interpreter.

## Where state lives

Outside the repository and outside the install, so upgrading the wheel
never disturbs it:

| Platform | Default |
|---|---|
| macOS / Linux | `~/.hyperbox/state/` |
| Windows | `%USERPROFILE%\.hyperbox\state\` |

Everything HyperBox owns lives under one directory:

| Directory | Holds |
|---|---|
| `~/.hyperbox/state/` | the SQLite registry and per-sandbox lock files |
| `~/.hyperbox/logs/` | `server.log`, rotated at 5 MB, three kept |
| `~/.hyperbox/environments/` | one directory per custom environment, each with a Dockerfile |

Override the state directory with `HYPERBOX_STATE_DIR`. Deleting it while
sandboxes are running orphans their containers — they are still labelled,
so `docker ps -a --filter label=hyperbox-mcp.managed=true` finds them.

Upgrading from v0.1 moves the registry across from
`~/.local/state/hyperbox-mcp/` on first start. The move is conservative:
it never overwrites a registry already at the new path, it moves only
`registry.db` and its WAL siblings, and it leaves both the lock files and
the old directory in place. `XDG_STATE_HOME` is no longer consulted — set
`HYPERBOX_STATE_DIR` if you want the registry somewhere specific.

## Upgrading

```bash
uv tool install --force hyperbox_mcp-0.1.1-py3-none-any.whl
```

The registry schema migrates forward in place, so sandboxes created by an
older version stay usable. Restart any MCP client afterwards — clients
launch the server once and hold it.

## Platform notes

**macOS.** Both engines work. Podman needs its machine running
(`podman machine start`). HyperBox finds the Podman CLI even when it is
not on `PATH`, and resolves the machine's unix socket itself — see
[troubleshooting.md](troubleshooting.md) for why that matters.

**Windows.** Docker is reached over `npipe:////./pipe/docker_engine`.
Podman is reached over its **Docker-compatible** named pipe, because the
Python Podman client has no named-pipe transport at all; HyperBox routes
around this automatically, and `hyperbox doctor` names the transport in
use. Podman Desktop must be running with its Docker-compatible endpoint
enabled.

**Any new machine.** Run this before anything else — it checks the host
side (locking, engine discovery, transports) and needs no container:

```bash
python tests/verify_platform.py
```

Then `hyperbox doctor`, then the full suites if you want the complete
picture.

## Rolling it out

A sensible order for a team:

1. Build the wheel once, from a known commit.
2. Install on one machine per platform you support; run
   `tests/verify_platform.py`, then `hyperbox doctor`.
3. Pre-pull the image during provisioning.
4. Ship the client config with an absolute executable path.
5. Keep the wheel and the commit that produced it together, so a report
   of odd behaviour can be traced to an exact build.
