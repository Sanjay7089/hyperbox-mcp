# sandbox-mcp — Requirements & Execution Plan

Source of truth for scope. Update this file before adding a tool or a
phase, not after. See `DESIGN.md` for non-goals and the architecture
decisions behind these choices.

## v1 tools (the sandbox lifecycle)

### `create_sandbox(language="python", backend="docker") -> dict`
- Creates and opens a persistent sandbox; returns
  `{sandbox_id, language, backend}` or `{error}`.
- `language`: `python`, `javascript` (v1). `backend`: `docker`,
  `podman` (both rootless).
- Invalid language/backend return a clear error BEFORE anything is
  allocated — never guess the environment.
- **Acceptance:** valid call returns a usable `sandbox_id`; invalid
  language/backend return an error and create no container.

### `run(sandbox_id, code, libraries=None, timeout=30) -> dict`
- Runs `code` in the named sandbox; returns
  `{stdout, stderr, exit_code, success}` or `{error}` for an
  unknown/dead sandbox.
- Repeatable on the same `sandbox_id`; state persists between calls.
- **Acceptance, all against a REAL container (never mocked):**
  1. Passing snippet → `success: true`, expected stdout.
  2. State persists: set a var in one call, read it in the next.
  3. Broken snippet (raised exception) → `success: false`, real
     traceback in `stderr`, non-zero `exit_code` — not swallowed.
  4. A snippet that exceeds `timeout` → `success: false` with the
     timeout legible in `stderr`. A timeout is a structured result like
     any other failure; it must never escape as an unhandled exception.

### `destroy_sandbox(sandbox_id) -> dict`
- Tears the sandbox down. Idempotent — destroying an already-gone
  sandbox is a success, not an error.
- Returns `{sandbox_id, status}` where `status` is `"destroyed"` or
  `"already_gone"`. Both are successes, so neither carries a boolean
  that reads as failure — an agent skimming for `false` must not
  conclude a working call failed.
- **Acceptance:** after destroy, the id is gone; a second destroy does
  not raise and reports `already_gone`.

## Non-functional requirements

- **The Runtime boundary is not optional.** The three tools talk only
  to the `Runtime` protocol (`runtime.py`). `llm_sandbox` may be
  imported ONLY in `llm_sandbox_runtime.py`. A `from llm_sandbox import`
  anywhere else is a bug — it breaks the replaceable-backend guarantee.
- No file is ever read from or written to the host by this server.
- No orchestration — sandboxes are created and destroyed on explicit
  tool calls, nothing auto-starts.
- `language` and `backend` are explicit, never auto-detected.
- Results are always structured; never return a bare failure string.

## Explicitly out of scope for v1 (see DESIGN.md)

`save()`, `read_file`, the agent/reasoning loop, chaos, cloud emulation,
Playwright, an MCP gateway, bash/shell execution, building our own
indexer or docs lookup.

---

## Execution plan (phases, owners, dependencies)

Each phase names the sub-agent that owns it (`.claude/agents/`). Phases
marked **parallel-safe** have no code dependency on each other and can
run as separate Claude Code sessions/sub-agents simultaneously.
**Sequential** phases must happen in order — don't let an agent start
one before the prior phase's acceptance criteria actually pass.

### Phase 0 — sequential, first, mostly human
Confirm this doc and `DESIGN.md` reflect current scope before any agent
writes code. (You're reviewing the result of that now.)

### Phase 1 — sequential — owner: `sandbox-engineer`
Get the lifecycle working for **Python only, Docker only**: the
`Runtime` protocol, the `LLMSandboxRuntime` implementation, and the
three tools. This is the one genuinely uncertain piece — everything
else is lower-risk wiring — so it's gated hard before anything builds
on it.
- The starter code is already written and verified to import, register
  exactly the three tools, satisfy the protocol, and fast-fail on bad
  input. The engineer's FIRST job is running `tests/verify.py` against
  a real Docker to confirm the container path works on this machine,
  then fixing whatever the real run surfaces.
- The language and backend maps ship holding only what has actually
  been verified — Phase 1 restricts them to `python` + `docker`. An
  entry is added only after a real run against that combination passes,
  so the tool can never hand back an environment nothing has exercised.
- **Gate:** don't start Phase 2 until you've personally watched a
  broken snippet come back with `success: false` and a real traceback
  from an actual container.

### Phase 2 — sequential (depends on Phase 1) — owner: `sandbox-engineer`
Two separate commits, not one:
1. Verify `backend="podman"` against rootless Podman specifically —
   don't assume the Docker path carries over.
2. Add `javascript`, then optionally `java`/`cpp`/`go`/`ruby`/`r` (all
   free from llm-sandbox) — each verified with a real run before it's
   added to the language map.

### Phase 3 — **parallel-safe** — owner: `mcp-composer`
Mount `mcp-code-indexer` (or `semantic-search-mcp`) under prefix
`index`, Context7 under `docs`, via `as_proxy()` + `mount()`. Confirm
each mounted tool round-trips a real call, not just that the process
starts.

### Phase 4 — **parallel-safe** — owner: `context-scaffolder`
A skill (not a tool) that scaffolds a starter `AGENTS.md` for a repo
without one. Small — not its own milestone.

### Cross-cutting — owner: `scope-guard`
Invoked before any agent builds something not already described here.

## v2 backlog
See `DESIGN.md`'s Backlog section.
