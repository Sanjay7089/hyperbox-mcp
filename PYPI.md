# HyperBox

**An MCP server that gives your coding agent somewhere to prove a change
actually works — before it tells you it does.**

Your agent says "done, tests pass." Did it actually run them? The same
model that wrote the code is also the one telling you it works, with no
independent check in between — which is exactly why AI-written diffs so
often look right and aren't.

HyperBox gives the agent somewhere to actually run the change:

```
create_sandbox()  →  run(code)  →  run(fixed code)  →  destroy_sandbox()
```

`run` returns real stdout, stderr and an exit code the agent didn't
generate and can't talk its way around — so the agent can fix its code
and try again, and "it works" stops being a claim you take on faith. As a
side effect, none of this touches your machine, your files, or your
credentials while it happens.

## What your agent gets

Four tools, and nothing else:

| Tool | What it does |
|---|---|
| `create_sandbox(language, backend, environment, packages, sync_from)` | A persistent, disposable container, with any declared packages installed before it is sealed, and optionally a directory of yours copied in. Returns a `sandbox_id`. |
| `run(sandbox_id, code, libraries, timeout)` | Executes code. Returns `{stdout, stderr, exit_code, success, timed_out}`. |
| `run(sandbox_id, code, background=True)` | Starts something that keeps running — a server, a worker — and returns a `process_id` instead of output. |
| `get_process_logs(sandbox_id, process_id)` | Reads what a background run has printed. |
| `destroy_sandbox(sandbox_id)` | Tears it down. Idempotent, and confirmed against the engine before claiming success. |

Plus a `hyperbox://capabilities` resource publishing the exact limits, so
an agent can read them instead of discovering them by failing.

**Five languages** — `python`, `javascript`, `bash`, `go` and `java` — each
on an official tagged image.

**Declare dependencies at creation.** `packages=["requests"]` installs while
the sandbox may still reach the network, which is then cut off for good.
`run(libraries=[...])` still works and returns a `deprecation` field, but it
has to reopen the network on an already-sealed sandbox.

Within one sandbox the filesystem and installed packages persist between
runs; variables do not, because each run is a fresh process. Write what
you need to keep to `/work`.

## What is enforced

Set by the server, not negotiable by the model, and **read back off the
real container** after creation — so a sandbox is never described as
limited when it is not:

| | |
|---|---|
| Memory | 1 GB, OOM-killed with a legible reason |
| CPU | 1 core |
| Processes | 128 PIDs |
| Timeout | 60 s ceiling |
| Network | detached before any submitted code runs |
| Host filesystem | never mounted |
| Container engine socket | never mounted |
| Scratch space | `/work`, 64 MB tmpfs, discarded with the sandbox |

Declared dependencies are the one network exception: they install in a
separate step that reattaches the network, runs a no-op program with the
package list, and detaches again before your code runs.

## Built on

- **[FastMCP](https://pypi.org/project/fastmcp/)** — the MCP server layer
  (stdio, JSON-RPC).
- **[llm-sandbox](https://pypi.org/project/llm-sandbox/)** — container
  session management, behind a `Runtime` protocol so the execution
  backend stays replaceable.
- **Docker or Podman** — whichever you have running. Both are supported
  and both pass the full acceptance suite.

Two runtime dependencies, no compiled extensions, one `py3-none-any`
wheel for every platform.

## Install

```bash
pip install hyperbox-mcp
```

Or, to get an isolated interpreter and a stable executable path — which
is what an MCP client needs:

```bash
uv tool install hyperbox-mcp     # or: pipx install hyperbox-mcp
```

Requires **Python 3.11+** and **Docker or Podman** running.

Check the machine and fetch the sandbox image once, so a multi-gigabyte
download never happens inside a client request:

```bash
hyperbox doctor --pull
```

## Use it: Cursor

Generate the config rather than typing it — the failure mode of getting a
path wrong is silent, with no tools appearing and no error anywhere:

```bash
hyperbox config --format cursor > .vscode/mcp.json
```

That writes:

```json
{
  "servers": {
    "hyperbox": {
      "command": "/Users/you/.local/bin/hyperbox",
      "args": [],
      "env": { "PATH": "/Users/you/.local/bin:/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

Restart Cursor — MCP configs are read at launch. Then ask it to run
something:

> Use hyperbox to check whether this regex handles the empty string.

A typical exchange looks like:

```
create_sandbox(language="python", packages=["requests"])
  → {"sandbox_id": "6f5eaaefb938", ...}

run(sandbox_id="6f5eaaefb938", code="import re; print(re.match(r'^\\d+$', ''))")
  → {"stdout": "None\n", "exit_code": 0, "success": true}

destroy_sandbox(sandbox_id="6f5eaaefb938")
  → {"status": "destroyed"}
```

### Other clients

```bash
hyperbox config --format json          # Claude Desktop, and most clients
hyperbox config --format antigravity   # Antigravity
hyperbox config --format yaml          # Continue-based clients
```

`PATH` in the generated config includes your container CLI's directory,
because clients launch servers with a trimmed environment and Docker is
frequently invisible otherwise.

## Custom environments

Start sandboxes from a heavier image so you do not pay a package install
every time:

```bash
hyperbox build data-science --dockerfile ./Dockerfile
hyperbox build torch --image pytorch/pytorch:latest     # or pull one
hyperbox envs
```

Your agent then asks for it by name:
`create_sandbox(environment="data-science")`. A running server picks up a
new environment without a restart.

Building is a CLI action on purpose. A build runs whatever the Dockerfile
says — arbitrary commands, as root, with network, under none of a
sandbox's limits — so an agent can *use* an environment but cannot create
one.

## Where the boundary sits

HyperBox is developer containment, not an isolation guarantee:

- **Local containers share your host's kernel.** No gVisor, no
  Firecracker, no VM boundary of its own.
- **Not a multi-tenant boundary.** Do not run untrusted third-party code
  as a service with it.
- **Code runs as root inside the container.** A non-root user breaks the
  execution backend; that root is confined by the container boundary,
  `no-new-privileges`, and the limits above.
- **Dependencies come from the public index** and are not vetted.

If you need a hard boundary for genuinely adversarial code, you want a VM
or microVM sandbox, not a local container.

## Commands

```
hyperbox                 Start the MCP server on stdio (default)
hyperbox doctor          Check this machine can run sandboxes
hyperbox config          Print a ready-to-paste MCP client config
hyperbox envs            List environments create_sandbox can use
hyperbox build <name>    Create an environment agents can select
                         (--dockerfile <path> or --image <ref>)
hyperbox logs            Show the server log
```

---

MIT licensed. Source, full documentation and issue tracker:
**[github.com/Sanjay7089/hyperbox-mcp](https://github.com/Sanjay7089/hyperbox-mcp)**
