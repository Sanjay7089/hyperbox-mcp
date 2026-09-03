# sandbox-mcp

An MCP server that gives any agent — Codeaira, Claude Code, Cursor, or
any MCP client — a disposable, persistent container to run code in
before it ever touches your real project. Create a sandbox, run in it
repeatedly (build → run → observe → fix → re-run), destroy it when done.

## Guiding principle

The LLM should not be the most trusted component; this server enforces
the boundaries. The agent proposes and reasons; the system controls
where code runs, what a result means, and how failure is represented.
See DESIGN.md.

## Read before doing anything

`REQUIREMENTS.md` has the full phased plan with owners and dependencies.
`DESIGN.md` has the non-goals and why each dependency was chosen. Read
both first — most "should we also add X" questions are already answered.

## The one architectural rule that must not break

The three lifecycle tools talk ONLY to the `Runtime` protocol
(`src/sandbox_mcp/runtime.py`). `llm_sandbox` may be imported ONLY in
`src/sandbox_mcp/llm_sandbox_runtime.py`. A `from llm_sandbox import`
anywhere else defeats the replaceable-backend design and is a bug. If
you're about to add one, stop and reconsider.

## Sub-agents — use them, don't route everything through the main thread

- **`sandbox-engineer`** — the Runtime protocol, the LLMSandboxRuntime
  impl, and the create/run/destroy tools. Phases 1-2, sequential.
- **`mcp-composer`** — mounting the codebase-indexer and Context7
  proxies. Phase 3, parallel-safe (independent of sandbox-engineer).
- **`context-scaffolder`** — the AGENTS.md-generation skill. Phase 4,
  parallel-safe.
- **`scope-guard`** — read-only; invoke before building anything not
  already in REQUIREMENTS.md.

Each building agent has a hard "do NOT" list in its own file — respect
it. If you're the orchestrating session: dispatch Phase 1 and wait for
its acceptance criteria to actually pass; dispatch Phases 3 and 4 in
parallel with it (different files, no shared state); hold Phase 2 until
Phase 1 passes.

## Stack

- Python 3.11+, package under `src/sandbox_mcp/`
- `fastmcp==2.14.7` pinned — see DESIGN.md's decision log for why not 4.x
- `llm-sandbox[docker,podman]` — behind the Runtime interface

## Commands

- Install: `uv sync` (creates `.venv/`, installs the project editable,
  writes `uv.lock` — commit the lockfile, never the venv)
- Run the server: `uv run python -m sandbox_mcp.server`
- Verify: the `verify-sandbox-mcp` skill, or
  `uv run python tests/verify.py`
  (`uv run python tests/verify.py podman` for the Podman path)

Always go through `uv run` — a bare `python` picks up whatever
interpreter is on PATH, not this project's pinned 3.11 environment.

## Testing

No mocked unit tests for the sandbox path — the acceptance criteria are
defined against a real container. Don't propose mocking Docker/Podman
as a substitute. When a container op fails, triage the layer (is the
engine running? socket reachable? image pullable?) before assuming the
code is broken — see the note at the top of tests/verify.py.

## Git workflow

- Never commit directly to `main`. Each phase or logical unit of work
  happens on its own branch: `phase-1-sandbox-lifecycle`,
  `phase-3-mcp-mounting`, etc. Open a PR into `main`; don't merge
  locally.
- Commit messages are 1–2 lines, imperative, describing the change and
  why — not a changelog. Good: "Add Podman backend to Runtime, verified
  against rootless socket". Bad: "updated files", "fixes", "wip".
- Commit in small, meaningful units — one commit per working increment,
  not one giant commit at the end and not a commit per file.
- Do NOT add "Co-Authored-By: Claude" or any tool attribution to
  commits. Do NOT change git author/committer config. Commits are
  authored by the repo owner — leave authorship untouched.
- Do NOT run `git push` or create PRs on your own — stage and commit
  only; the owner reviews and pushes.