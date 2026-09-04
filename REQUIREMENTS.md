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
- Repeatable on the same `sandbox_id`. What persists between calls is
  the SANDBOX — the container, its filesystem, and any packages
  installed via `libraries` — not interpreter memory: each `run` is a
  fresh process. Interpreter memory is documented as not guaranteed
  rather than guaranteed absent, so a future kernel-backed Runtime
  could offer more without breaking this contract.
- **Acceptance, all against a REAL container (never mocked):**
  1. Passing snippet → `success: true`, expected stdout.
  2. The sandbox persists: a file written in one call is readable in
     the next, and a package installed via `libraries` in one call is
     importable in a later call that does not name it. (This replaced
     a variable-persistence check that llm-sandbox cannot satisfy —
     see DESIGN.md's Decision Log, 2026-09-04.)
  3. Broken snippet (raised exception) → `success: false`, real
     traceback in `stderr`, non-zero `exit_code` — not swallowed.
  4. A snippet that exceeds `timeout` → `success: false` with the
     timeout legible in `stderr`. A timeout is a structured result like
     any other failure; it must never escape as an unhandled exception.
  5. `timeout=None` is REJECTED, and any requested timeout above the
     server cap is clamped to it. Execution time is server policy, not
     an agent's choice.
  6. `stdout`/`stderr` are capped and explicitly marked when truncated,
     so a single run cannot fill the caller's context window.

### `destroy_sandbox(sandbox_id) -> dict`
- Tears the sandbox down. Idempotent — destroying an already-gone
  sandbox is a success, not an error.
- Returns `{sandbox_id, status}` where `status` is `"destroyed"` or
  `"already_gone"`. Both are successes, so neither carries a boolean
  that reads as failure — an agent skimming for `false` must not
  conclude a working call failed.
- `already_gone` must reflect the REAL container state, not merely an
  absent in-process handle. A container that is still running must
  never be reported as gone.
- **Acceptance:** after destroy, the id is gone AND the container is
  actually stopped; a second destroy does not raise and reports
  `already_gone`.

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

### Phase 5 — sequential — owner: `sandbox-engineer`
**Persistent sandbox registry.** Sandbox ownership currently lives only
in one Python process (`_handles` in server.py, `_sessions` in
llm_sandbox_runtime.py), so a restart, a duplicate client launch, or a
second MCP process loses the handle while leaving the container running.
Diagnostics confirmed all three: a stale id after restart, a 5/10 failure
rate alternating between two live server processes, and orphaned
containers reported as `already_gone` while still up.

- Registry in SQLite holding `sandbox_id`, container id, language,
  backend, created-at, last-used-at, expiry.
- Every managed container labelled `sandbox-mcp.managed=true` and
  `sandbox-mcp.id=<id>`.
- A new process reopens a sandbox from its stored container id rather
  than an in-memory object. llm-sandbox supports this via
  `container_id=` / `_connect_to_existing_container`.
- Per-sandbox lock so concurrent calls cannot corrupt one session.
- Startup and periodic GC destroying expired or lost containers —
  matching OUR labels only, never a container we did not create.
- **Acceptance:** create → kill the server → run and destroy through a
  NEW process; two concurrent processes share one sandbox safely;
  `destroy_sandbox` reports `already_gone` only when the container is
  genuinely gone.

### Phase 6 — sequential (depends on Phase 5) — owner: `sandbox-engineer`
**Resource policy enforced in the runtime, never in prompts.** Diagnostics
measured a container OOM-killed at ~1.6 GB reporting only a bare
`exit_code 137` with empty stderr, and `timeout=None` running unbounded
for 123 s.

- Per-sandbox `mem_limit`, CPU cap, and PID cap.
- Network disabled by default (`sealed`); a scoped `build` phase enables
  network only to install declared `libraries`, then returns to sealed.
- TTL on inactivity; capped stdout/stderr with truncation marked.
- No host filesystem or container-engine socket may be mounted.
- **Acceptance:** a memory bomb is killed at the configured ceiling with
  a legible reason (not a bare 137); a CPU/PID bomb is contained; network
  is unreachable under `sealed`; oversized output is truncated and marked.

### Cross-cutting — owner: `scope-guard`
Invoked before any agent builds something not already described here.

## v2 backlog
See `DESIGN.md`'s Backlog section.
