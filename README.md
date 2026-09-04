# sandbox-mcp

An MCP server that gives any agent — Codeaira, Claude Code, Cursor, or
any MCP client — a disposable, persistent container to run code in
before it touches your real project. The agent creates a sandbox, runs
code in it repeatedly (build → run → observe the failure → fix →
re-run), and destroys it when done. It writes the final, working code
to your project using its *own* editing tools — not ours.

**Guiding principle:** the LLM should not be the most trusted
component. The agent reasons and proposes; this server controls where
code runs and what a result means.

## The three tools

- `create_sandbox(language, backend)` → a persistent sandbox id
- `run(sandbox_id, code, ...)` → `{stdout, stderr, exit_code, success}`,
  callable repeatedly; the sandbox's filesystem and installed packages
  persist between calls (each call is a fresh process)
- `destroy_sandbox(sandbox_id)` → tear it down (idempotent)

That's v1. Chaos testing, cloud emulation, and browser testing are
deliberately deferred — see `DESIGN.md`.

## Quickstart

```bash
uv sync
uv run python -m sandbox_mcp.server   # stdio MCP server
```

Point any MCP client at `uv run python -m sandbox_mcp.server`, then:

> Create a Python sandbox, write a function to parse this log line, run it, and fix it until it works. Then destroy the sandbox.

## Architecture (why this isn't just a wrapper)

The three tools talk to a `Runtime` protocol, never to the execution
backend directly. `llm-sandbox` is one implementation of that protocol,
living in a single file (`llm_sandbox_runtime.py`) — the only file
allowed to import it. Swap that one file (for Firecracker, a raw
podman-py wrapper, anything) and the MCP layer never notices. The
contract and the abstraction are the project's IP; the current backend
is replaceable.

## How this repo is meant to be built (with Claude Code)

The whole destination is documented up front; the work is divided into
independently executable sub-agent tasks with explicit dependencies.
Plan, owners, and dependencies live in `REQUIREMENTS.md`; each owner is
a sub-agent in `.claude/agents/`, and each has a hard "do NOT" list to
stop scope creep.

- `sandbox-engineer` → Runtime + lifecycle tools (Phases 1-2, sequential)
- `mcp-composer` → mount indexer + Context7 (Phase 3, parallel-safe)
- `context-scaffolder` → AGENTS.md skill (Phase 4, parallel-safe)
- `scope-guard` → read-only scope check before building anything new

Suggested first session (orchestrating thread):

> Read CLAUDE.md, REQUIREMENTS.md, and DESIGN.md. Do not write code yet — validate the plan is internally consistent and flag any contradiction. Then dispatch Phase 1 to sandbox-engineer, and Phases 3 and 4 to mcp-composer and context-scaffolder in parallel. Do not let Phase 2 start until Phase 1's acceptance criteria pass against a real container.

The Phase 1 starter code is already written and verified to import,
register exactly the three tools, satisfy the Runtime protocol, and
fast-fail on bad input. The container path itself needs your real
Docker/Podman — that's Phase 1's gate.

## The gate that matters

Before moving to the bigger roadmap, this must work reliably:

```
create_sandbox() → run(broken code) → success:false + real traceback
  → agent fixes → run() → success:true → destroy_sandbox()
```

If that loop works against a real container, you have the foundation.

## Verify

```bash
uv run python tests/verify.py           # against Docker
uv run python tests/verify.py podman    # against rootless Podman
```

Runs the real lifecycle against a real container: create, a passing
run, sandbox persistence, a deliberately-broken run, a timeout, and
idempotent destroy. "Done" means this passes.

## Docs

- `DESIGN.md` — principle, non-goals, and why each dependency was chosen
- `REQUIREMENTS.md` — the tool specs and the full phased plan
- `CLAUDE.md` — instructions Claude Code reads every session

## Push to your clean repo

The repo is already initialized, with the scaffold as its first commit
on `main`. To publish it:

```bash
gh repo create sandbox-mcp --public --source=. --remote=origin
git push -u origin main
```

After that first push, nothing lands on `main` directly — each phase
gets its own branch and a PR. See `CLAUDE.md`.

## License

MIT — see `LICENSE`.
