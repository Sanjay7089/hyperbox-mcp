# HyperBox — Design Doc

Living document. Any scope or architecture change gets a line in the
Decision Log before it gets code.

## Problem

An AI coding agent (Codeaira, Claude Code, Cursor, or any MCP client)
can write code, but it can't safely run it before handing it to you.
Today that means manually pasting the result into your project to find
out whether it works. This project gives any MCP-speaking agent one
thing: a disposable, persistent computer to run code in — so it can
build, run, observe the failure, fix, and re-run until it works,
before you ever see it.

## Guiding principle

**The LLM should not be the most trusted component; the surrounding
system enforces the boundaries.** The agent is good at understanding
the task, reasoning about code, and deciding what to try next.
Deterministic infrastructure — this server — controls where code runs,
what a result means, and how failure is represented. The agent
proposes; the system executes and observes. Keep that separation
intact: nothing the agent says should let it run code anywhere except
inside a sandbox this server created.

## Non-goals

- **Writing files back to your project.** Claude Code, Codeaira, and
  Cursor already have their own file-write/accept flow. Not ours to
  duplicate. (A `save()` for MCP clients lacking one is parked in the
  backlog, unbuilt until a real host needs it.)
- **Reading project files.** Same reason — the host already does this.
- **The reasoning / agent loop itself** (issue → plan → change → PR).
  That's the host's job. This server is the environment the agent
  reasons *inside*, not the reasoner. Building the loop here would be
  rebuilding the host.
- **Chaos testing, cloud emulation, browser/e2e testing.** Real,
  deferred — see backlog.
- **Building our own execution engine, codebase indexer, or docs
  lookup.** All exist, maintained, open source. We glue.
- **An MCP gateway/aggregator.** Every client we target connects to
  multiple servers natively. Nothing to build at this scale.

## Core decisions

### The Runtime interface is the actual IP
Everything above `runtime.py` (the MCP tools, the handle registry)
talks to a `Runtime` protocol — `create` / `run` / `destroy` — never to
a backend directly. llm-sandbox is ONE implementation
(`llm_sandbox_runtime.py`), the only file allowed to import it. Swap
that one file (for Firecracker, a raw podman-py wrapper, whatever) and
the MCP layer never notices. This is what stops the project from being
"a thin wrapper around llm-sandbox" — the contract and the abstraction
are ours; the current backend is replaceable.

### Persistent sandboxes, not one-shot runs
Three tools: `create_sandbox` → `run` (repeatable) → `destroy_sandbox`.
A sandbox is created once and run in many times, so the environment
persists across the build→run→observe→fix→re-run loop. Verified
directly that llm-sandbox sessions expose explicit `.open()` /
`.close()`, so holding one open across calls is supported — not a hack.

Be precise about what that buys, because the imprecise version of this
sentence produced a wrong acceptance criterion: session-open gives
continuity of the container, its filesystem, and installed packages.
Each `run()` is still a fresh process, so interpreter memory does not
carry over. The loop this section protects doesn't need it to — the
agent resubmits corrected code rather than depending on a live
variable.

### Structured results, never bare strings
`run` returns `{stdout, stderr, exit_code, success}`. The agent has to
reason about *what* failed to fix it; "Command failed" is useless to a
reasoning layer. This is a load-bearing design choice, not cosmetics.

### Both Docker and Podman, both rootless
Podman is rootless by default. Docker rootless has been stable since
20.10 (2020), improved in v29.5 (2026). Neither needs privileged
containers for plain code execution. Rootless is the default
assumption for both.

### Composition via FastMCP's own mount(), pinned to 2.14.7
`FastMCP.as_proxy()` + `mount()` — verified directly against
`fastmcp==2.14.7`. 4.x reorganizes proxying around a
ProxyProvider/client_factory pattern that's real but under-documented
and more fragile; not worth the risk. Re-evaluate the pin deliberately,
don't drift onto 4.x by accident.

### Adopt, don't build: indexing, docs, project context
- Codebase indexing → mount `mcp-code-indexer` (Qdrant, call-graph,
  git-aware) for large repos, or `semantic-search-mcp` (pure local) for
  small ones. Both already do AST/tree-sitter structural chunking and
  hybrid BM25+vector search — the right way to retrieve code, and not
  ours to reimplement.
- Docs/best-practice → mount Context7.
- Project context across sessions → adopt the AGENTS.md standard, not a
  custom memory system. (Kilo Code shipped a bespoke "memory bank" for
  this and deprecated it in favor of plain AGENTS.md — the field
  already tried the fancier version.)

## Architecture

```
LLM / agent (Codeaira, Claude Code, Cursor, any MCP client)
        │ MCP
        ▼
HyperBox (FastMCP, Python)
   ├── create_sandbox / run / destroy_sandbox
   │        │ (talks only to the Runtime protocol)
   │        ▼
   │   Runtime  ◄── llm_sandbox_runtime.py (replaceable impl)
   │                    │
   │                    ▼  Docker or Podman (rootless) container
   ├── (mount) index_*  ────► mcp-code-indexer / semantic-search-mcp
   └── (mount) docs_*   ────► Context7
```

## Decision log

| Date | Decision | Why |
|---|---|---|
| Reset | Runtime protocol in front of llm-sandbox | Backend must be replaceable; the contract + abstraction are the IP, not the wrapper |
| Reset | Persistent create/run/destroy, not one-shot run() | The real workflow is build→run→observe→fix→re-run in ONE environment |
| Reset | Structured results {stdout,stderr,exit_code,success} | A reasoning agent must know what failed, not just that it failed |
| Reset | Chaos/cloud/browser out of core | Real problems, wrong slice for v1 — backlog |
| Reset | fastmcp pinned 2.14.7, not 4.x | Verified 2.x proxy/mount API directly; 4.x rework under-documented |
| Reset | No bash/shell in run() | Not a llm-sandbox SupportedLanguage; separate execute_command tool later if needed |
| 2026-09-04 | uv for env + `uv.lock` committed | Reproducible installs; `.python-version` pins the 3.11 floor so we develop against the minimum we claim to support |
| 2026-09-04 | CLAUDE/DESIGN/REQUIREMENTS + `.claude/` tracked in git | They are the declared source of truth; a reviewer must see the requirement change behind a code change. Only `settings.local.json` stays ignored |
| 2026-09-04 | Phase 3 mounting lives in `mounts.py`, not `server.py` | mcp-composer was barred from server.py yet had to mount onto its FastMCP instance; `mounts.register(mcp)` keeps Phases 1 and 3 file-disjoint and genuinely parallel |
| 2026-09-04 | Sandbox ownership moves to a SQLite registry + container labels | In-process handles cannot survive a restart or a second server process. Measured: 5/10 calls fail when alternating between two live processes, and killed servers orphan running containers that destroy_sandbox reports `already_gone`. Reattach is possible because llm-sandbox exposes `container_id=` / `_connect_to_existing_container` (docker.py:287-312) |
| 2026-09-04 | Resource limits become server policy; `timeout=None` rejected | Measured unbounded: a 120s sleep ran to 123s, and a memory bomb reached ~1.6GB before the VM OOM-killer produced a bare exit 137 with empty stderr. `runtime_configs` reaches `containers.create()` verbatim (docker.py:378, 33), so mem/CPU/PID/network caps need no upstream change |
| 2026-09-04 | Language/backend maps ship restricted to what's verified | Code had shipped javascript + podman ahead of their phase gate, so create_sandbox could hand back an environment nothing had ever run |
| 2026-09-04 | "Persistent" narrowed to container + filesystem + installed packages; interpreter memory documented as not guaranteed | The original criterion ("set a var in one call, read it in the next") encoded a wrong assumption about llm-sandbox's execution model, not a bug. Measured against a real container: both calls share one container (identical `os.uname().nodename`) but run as different processes (`os.getpid()` 45 then 58), because each snippet is executed as its own `/sandbox/<uuid>.py`. A file written in one call and a package installed in one call both survive into later calls. Narrowed rather than negated, so a future kernel-backed Runtime stays conformant |

## Backlog (parked, not started)

- `save()` for MCP clients without their own file-write flow
- Chaos sidecar via Pumba (kill + netem, zero app-side setup)
- LocalStack (AWS) / GCP emulators for IaC workflows
- Playwright MCP for frontend/e2e
- Raw shell via execute_command() as its own tool
- Additional Runtime implementations (e.g. Firecracker/microVM)
