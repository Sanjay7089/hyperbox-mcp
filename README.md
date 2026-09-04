# HyperBox

**Your AI agent can run terminal commands. HyperBox gives it somewhere safe to do that.**

Claude Code, Cursor, and any agent with shell access will happily execute
the code they just wrote — on your machine, against your files, with your
credentials. Usually that's fine. Occasionally it's `rm -rf`, a global
install that breaks another project, or a script that quietly talks to
production.

HyperBox is an MCP server that gives the agent a disposable container to
run code in first, and — the part that actually matters — makes the agent
*understand* when to use it.

## It contains hostile code. Here's the proof, not the promise.

`tests/verify_containment.py` runs genuinely dangerous code in a real
container. Actual output:

```
sandbox: bf4ea26ec5a5

PASS  host filesystem is unreachable from inside
        stdout: 'DENIED: FileNotFoundError'
PASS  network is unreachable from inside
        stdout: 'DENIED: OSError'
PASS  container engine socket is not mounted
        stdout: 'docker.sock present: False'
PASS  memory exhaustion is capped, with a legible reason
        exit_code: 137 | stderr: Killed (SIGKILL): the sandbox exceeded its memory limit of 1g.
PASS  process explosion is capped by the PID limit
        stdout: 'DENIED after 126 processes: BlockingIOError'
PASS  sandbox is destroyed cleanly afterwards

6/6 contained
```

The fork bomb stopped at 126 processes against a ceiling of 128. Run it
yourself — that is the point of shipping it as a test.

## Quickstart

```bash
uv sync
uv run python -m hyperbox_mcp.server     # stdio MCP server
```

Then point an MCP client at it. For Claude Desktop, in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "HyperBox": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--project", "/absolute/path/to/hyperbox",
               "python", "-m", "hyperbox_mcp.server"],
      "env": { "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" }
    }
  }
}
```

Requires Docker or rootless Podman running. On macOS with Podman, export
`CONTAINER_HOST` from `podman machine inspect` first — without it
podman-py connects but silently returns empty output.

## The tools

| Tool | What it does |
|---|---|
| `create_sandbox(language, backend)` | A persistent, disposable container. Returns a `sandbox_id`. |
| `run(sandbox_id, code, libraries, timeout)` | Executes code. Returns `{stdout, stderr, exit_code, success}` — never a bare "it failed". |
| `destroy_sandbox(sandbox_id)` | Tears it down. Idempotent, and verified against the engine before it claims success. |

Languages: `python`, `javascript`, `ruby`, `go`. Backends: `docker`,
`podman`. Each combination is in the map only after passing the suite
against a real container — an entry is a promise the tool can deliver it.

## What the sandbox enforces

Set by the server, not negotiable by the agent:

| | |
|---|---|
| Memory | 1 GB, OOM-killed with a legible reason rather than a bare exit 137 |
| CPU | 1 core |
| Processes | 128 PIDs |
| Network | **disabled while your code runs** |
| Timeout | capped at 60s; `timeout=None` is rejected |
| Output | capped per stream, and marked when truncated |
| Host FS / engine socket | never mounted |

Declaring `libraries=[...]` opens the network for a separate install step,
then re-seals before your code executes.

## Why this isn't a thin wrapper

**The agent has to understand it, or it won't use it.** An MCP client has
no dispatcher — it decides from tool names, descriptions and annotations
alone. Measured here: a working search tool described only as "search the
codebase" was *refused* by a client that asked the user to upload files by
hand; naming what it covered turned the same tool into a correct answer.
So HyperBox ships tool annotations (`run` is marked not host-destructive;
`destroy_sandbox` destructive but idempotent), a `hyperbox://capabilities`
resource so limits are discoverable before they're hit, and a `run_safely`
prompt that makes the safe path the easy path.

**Sandboxes outlive the process that made them.** Ownership lives in a
SQLite registry outside the repo, and containers carry
`hyperbox-mcp.managed` labels. A restarted server — or a second one your
client launched — reattaches by container id instead of losing the
sandbox and leaking the container. Before this existed, alternating calls
between two server processes failed 5 times in 10.

**The backend is replaceable.** Everything above `runtime.py` talks to a
`Runtime` protocol. `llm-sandbox` is one implementation, in the only file
allowed to import it. The contract is the project; the backend is a
detail.

## Verify

```bash
uv run python tests/verify.py              # 14 — lifecycle + MCP surface
uv run python tests/verify_registry.py     # 11 — ownership across processes
uv run python tests/verify_limits.py       # 12 — enforced resource policy
uv run python tests/verify_containment.py  #  6 — the safety proof
uv run python tests/verify.py podman       # 9  — second backend
```

43 checks, all against real containers. There are no mocked tests for the
sandbox path on purpose: mocking Docker would prove only that the mock
works.

## License

MIT — see `LICENSE`.
