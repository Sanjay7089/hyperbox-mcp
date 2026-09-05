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
2. **The sandbox image**, pulled on first use (~1.6 GB). To avoid a slow
   first call, pre-pull during provisioning:

   ```bash
   hyperbox doctor --pull
   ```

3. Nothing else. No Python on `PATH` is required by the client: the tool
   install carries its own interpreter.

## Where state lives

Outside the repository and outside the install, so upgrading the wheel
never disturbs it:

| Platform | Default |
|---|---|
| macOS / Linux | `~/.local/state/hyperbox-mcp/` |
| Windows | `%USERPROFILE%\.local\state\hyperbox-mcp\` |

Override with `HYPERBOX_STATE_DIR`. It holds a small SQLite registry and
per-sandbox lock files. Deleting it while sandboxes are running orphans
their containers — they are still labelled, so
`docker ps -a --filter label=hyperbox-mcp.managed=true` finds them.

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
