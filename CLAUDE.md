# HyperBox

An MCP server that gives any agent — Claude Code, Claude Desktop, Cursor,
or any MCP client — a disposable container to run code in before it
touches the user's machine. Create a sandbox, run in it repeatedly
(build → run → observe → fix → re-run), destroy it when done.

## Guiding principle

The LLM should not be the most trusted component; this server enforces
the boundaries. The agent proposes and reasons; the system controls where
code runs, what a result means, and how failure is represented. See
DESIGN.md.

## The two rules that must not break

**1. The Runtime boundary.** The lifecycle tools talk ONLY to the
`Runtime` protocol (`src/hyperbox_mcp/runtime.py`). `llm_sandbox` may be
imported ONLY in `src/hyperbox_mcp/llm_sandbox_runtime.py`. A
`from llm_sandbox import` anywhere else defeats the replaceable-backend
design and is a bug.

**2. Limits are server policy, never the agent's choice.** Memory, CPU,
PIDs, network posture and the timeout ceiling are set in the runtime. Do
not add a tool parameter that lets a caller raise them.

## Descriptions are the routing logic

An MCP client has no dispatcher. It decides whether to call a tool purely
from the tool's name, description and annotations. This was measured: a
working search tool described only as "search the codebase" was refused by
a client that asked the user to upload files by hand; naming what it
covered turned the same tool into a correct answer.

So prose in `server.py` is load-bearing. When changing a tool, say what it
is for AND when NOT to use it, and keep the annotations honest.

## Stack

- Python 3.11+, package under `src/hyperbox_mcp/`
- `fastmcp>=4.0.2` — note 4.x removed `FastMCP.as_proxy`, renamed
  `get_tools` to `list_tools`, and no longer wraps decorated tools (so
  `.fn` is gone; the function itself is callable)
- `llm-sandbox[docker,podman]` — behind the Runtime interface

## Commands

- Install: `uv sync`
- Run the server: `uv run python -m hyperbox_mcp.server`
- Verify: the `verify-hyperbox` skill, or the suites below

Always go through `uv run` — a bare `python` is the system interpreter
with none of this project's dependencies.

## Testing

```bash
uv run python tests/verify.py              # lifecycle + MCP surface
uv run python tests/verify_registry.py     # ownership across processes
uv run python tests/verify_limits.py       # enforced resource policy
uv run python tests/verify_containment.py  # the safety proof
uv run python tests/verify.py podman       # second backend
```

No mocked tests for the sandbox path — acceptance is defined against a
real container, because mocking Docker would prove only that the mock
works. When a container op fails, triage the layer (is the engine running?
socket reachable? image pullable?) before assuming the code is broken —
see the note at the top of `tests/verify.py`.

An entry in the language or backend map is a promise the tool can deliver
that environment. Nothing goes in until the suite passes for it against a
real container.

## Git workflow

- Never commit directly to `main`. Each logical unit of work happens on
  its own branch. Open a PR into `main`; don't merge locally.
- Commit messages are 1–2 lines, imperative, describing the change and
  why — not a changelog.
- Commit in small, meaningful units — one commit per working increment.
- Do NOT add "Co-Authored-By: Claude" or any tool attribution to commits,
  and do NOT change git author/committer config.
- Do NOT run `git push` or create PRs on your own — stage and commit only.
