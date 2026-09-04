# HyperBox — Design

Living document. Any scope or architecture change gets a line in the
Decision Log before it gets code.

## Problem

An AI coding agent (Claude Code, Claude Desktop, Cursor, or any MCP
client) can already run terminal commands and edit files — directly on
your machine. That is useful right up until the code it just wrote does
something you would not have approved: deletes the wrong tree, installs
globally, or reaches a service it should not.

HyperBox gives that agent a disposable computer to run code in, and makes
the agent understand when to use it.

## Guiding principle

**The LLM should not be the most trusted component; the surrounding
system enforces the boundaries.** The agent is good at understanding the
task and deciding what to try next. Deterministic infrastructure — this
server — controls where code runs, what a result means, and how failure
is represented. Nothing the agent says should let it run code anywhere
except inside a sandbox this server created, or raise a limit this server
set.

## Non-goals

- **Writing files back to your project.** The host already has its own
  file-write flow. Not ours to duplicate.
- **Reading project files.** Same reason.
- **The reasoning / agent loop itself.** This server is the environment
  the agent reasons *inside*, not the reasoner.
- **Bundling other people's MCP servers.** Tried and measured: a mounted
  docs proxy cost 59% of the tool-definition budget on every message and
  was called zero times. Connect those servers directly to your client
  instead; they do not belong inside this one.
- **Building our own execution engine.** llm-sandbox exists and is
  maintained. We own the contract, not the container plumbing.
- **Chaos testing, cloud emulation, browser/e2e testing.** Real problems,
  wrong project.

## Core decisions

### The Runtime interface is the actual IP
Everything above `runtime.py` talks to a `Runtime` protocol —
`create` / `run` / `destroy` / `alive` / `gc` — never to a backend
directly. llm-sandbox is ONE implementation
(`llm_sandbox_runtime.py`), the only file allowed to import it. Swap that
file and the MCP layer never notices.

### Sandboxes outlive the process that created them
Ownership lives in a SQLite registry outside the repo; containers carry
`hyperbox-mcp.managed` / `hyperbox-mcp.id` labels; a new process
reattaches by container id. Measured before this existed: alternating
calls between two live server processes failed 5/10, and killed servers
orphaned running containers that `destroy_sandbox` called `already_gone`.

### Limits are server policy, not agent choice
1 GB memory, 1 CPU, 128 PIDs, network sealed while caller code runs, 60s
timeout ceiling, capped output. An agent cannot raise any of them. A
`libraries` install briefly opens the network, then re-seals.

### Comprehension is a feature, not documentation
An MCP client has no dispatcher — it routes on tool names, descriptions
and annotations alone. Measured: a working tool described as "search the
codebase" was refused by a client that asked the user to upload files;
naming what it covered made the same tool work. So HyperBox ships
annotations, a `hyperbox://capabilities` resource, and a `run_safely`
prompt as first-class parts of the product.

### Structured results, never bare strings
`run` returns `{stdout, stderr, exit_code, success}`. An agent has to know
*what* failed to fix it. Even an OOM kill is translated from a bare exit
137 into a sentence naming the limit it hit.

## Tool contract

### `create_sandbox(language="python", backend="docker") -> dict`
Returns `{sandbox_id, language, backend, next}` or `{error}`. Invalid
language/backend fail before anything is allocated. Languages and backends
appear in the map only after the suite passes for them against a real
container.

### `run(sandbox_id, code, libraries=None, timeout=30) -> dict`
Returns `{stdout, stderr, exit_code, success}` or `{error}`. Repeatable
on one sandbox. The container, its filesystem and installed packages
persist between calls; interpreter memory does not — each run is a fresh
process. `timeout=None` is rejected; values above the ceiling are clamped.

### `destroy_sandbox(sandbox_id) -> dict`
Returns `{sandbox_id, status}` where status is `destroyed` or
`already_gone` — both successes, so no field says `false` on a call that
worked. `already_gone` is confirmed against the engine, not inferred from
bookkeeping.

## Architecture

```
LLM / agent (Claude Code, Claude Desktop, Cursor, any MCP client)
        │ MCP
        ▼
HyperBox (FastMCP, Python)
   ├── create_sandbox / run / destroy_sandbox
   ├── hyperbox://capabilities  (discoverable limits)
   ├── run_safely               (prompt)
   │        │ (talks only to the Runtime protocol)
   │        ▼
   │   Runtime  ◄── llm_sandbox_runtime.py (replaceable impl)
   │                    │
   │                    ▼  Docker or Podman (rootless) container
   └── registry.py (SQLite, cross-process ownership + labelled GC)
```

## Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-09-04 | Dropped the index/docs mounts; sandbox only | Measured: docs_* cost ~1163 tok/message (59% of the tool budget) for zero calls; the mounts also owned the entire index-staleness bug class. Bundling other servers added surface, not value |
| 2026-09-04 | fastmcp 2.14.7 → >=4.0.2 | The pin existed only to protect `as_proxy`, which the mount removal deletes. 2.14.7 was the FINAL 2.x release (2026-04-13) — shipping OSS on a dead major is a liability. 4.x drops `as_proxy`, renames `get_tools`→`list_tools`, and no longer wraps decorated tools. All 43 checks pass on 4.0.2 |
| 2026-09-04 | Comprehension layer: annotations + capabilities resource + prompt | A client refused a working tool because its description did not say what it covered. Descriptions and annotations ARE the routing logic; there is no dispatcher to fix it anywhere else |
| Reset | Runtime protocol in front of llm-sandbox | Backend must be replaceable; the contract + abstraction are the IP, not the wrapper |
| Reset | Persistent create/run/destroy, not one-shot run() | The real workflow is build→run→observe→fix→re-run in ONE environment |
| Reset | Structured results {stdout,stderr,exit_code,success} | A reasoning agent must know what failed, not just that it failed |
| Reset | Chaos/cloud/browser out of core | Real problems, wrong slice for v1 — backlog |
| Reset (SUPERSEDED 2026-09-04) | fastmcp pinned 2.14.7, not 4.x | Verified 2.x proxy/mount API directly; 4.x rework under-documented |
| Reset | No bash/shell in run() | Not a llm-sandbox SupportedLanguage; separate execute_command tool later if needed |
| 2026-09-04 | uv for env + `uv.lock` committed | Reproducible installs; `.python-version` pins the 3.11 floor so we develop against the minimum we claim to support |
| 2026-09-04 | CLAUDE/DESIGN/REQUIREMENTS + `.claude/` tracked in git | They are the declared source of truth; a reviewer must see the requirement change behind a code change. Only `settings.local.json` stays ignored |
| 2026-09-04 (SUPERSEDED) | Phase 3 mounting lives in `mounts.py`, not `server.py` | mcp-composer was barred from server.py yet had to mount onto its FastMCP instance; `mounts.register(mcp)` keeps Phases 1 and 3 file-disjoint and genuinely parallel |
| 2026-09-04 (SUPERSEDED) | `docs_` (Context7) mount is OFF by default | Measured: docs_* adds ~1163 tokens of tool definitions to EVERY message — 59% of the whole tool budget — and was called zero times across a full evaluation session, while index_* cost ~289 tokens and did all the useful work. Enable with HYPERBOX_MCP_DOCS_URL, or connect Context7 as its own MCP server so its cost is not paid inside every HyperBox request |
| 2026-09-04 (SUPERSEDED) | Index search results carry a freshness warning | The backing indexer serves its cache forever with no staleness check, so a search can confidently return a deleted path — observed returning `src/sandbox_mcp/server.py` for 9 hours after that file was renamed. A debounced mtime walk cannot make the index fresh, but it makes it honest, which is what an agent reasoning on the result needs |
| 2026-09-04 | mcp-code-indexer REJECTED (correcting the earlier entry) | The earlier description of it as "Qdrant, call-graph, git-aware" was wrong on the facts. v4.2.20 depends on turbopuffer (hosted vector DB) and voyageai (hosted embeddings), and its Q&A path needs OPENROUTER_API_KEY — three external SaaS credentials, with source code leaving the machine. That contradicts the sealed-network posture of Phase 6. It is also description-based (agent-authored file summaries via update_file_description), not code embeddings, so it solves navigation rather than "where is X implemented" |
| 2026-09-04 (SUPERSEDED) | Phase 3 mounts semantic-search-mcp (not mcp-code-indexer) under `index`, Context7 under `docs` | Both were sanctioned options; the local one needs no Qdrant service, so `python -m hyperbox_mcp.server` stays runnable with nothing else set up. Overridable via HYPERBOX_MCP_INDEX_CMD / HYPERBOX_MCP_DOCS_URL, either set to "off" to skip that mount |
| 2026-09-04 | Sandbox ownership moves to a SQLite registry + container labels | In-process handles cannot survive a restart or a second server process. Measured: 5/10 calls fail when alternating between two live processes, and killed servers orphan running containers that destroy_sandbox reports `already_gone`. Reattach is possible because llm-sandbox exposes `container_id=` / `_connect_to_existing_container` (docker.py:287-312) |
| 2026-09-04 | Resource limits become server policy; `timeout=None` rejected | Measured unbounded: a 120s sleep ran to 123s, and a memory bomb reached ~1.6GB before the VM OOM-killer produced a bare exit 137 with empty stderr. `runtime_configs` reaches `containers.create()` verbatim (docker.py:378, 33), so mem/CPU/PID/network caps need no upstream change |
| 2026-09-04 | Language/backend maps ship restricted to what's verified | Code had shipped javascript + podman ahead of their phase gate, so create_sandbox could hand back an environment nothing had ever run |
| 2026-09-04 | "Persistent" narrowed to container + filesystem + installed packages; interpreter memory documented as not guaranteed | The original criterion ("set a var in one call, read it in the next") encoded a wrong assumption about llm-sandbox's execution model, not a bug. Measured against a real container: both calls share one container (identical `os.uname().nodename`) but run as different processes (`os.getpid()` 45 then 58), because each snippet is executed as its own `/sandbox/<uuid>.py`. A file written in one call and a package installed in one call both survive into later calls. Narrowed rather than negated, so a future kernel-backed Runtime stays conformant |

## Backlog (parked, not started)

- Additional Runtime implementations (e.g. Firecracker/microVM)
- Raw shell via an `execute_command()` tool of its own
- `save()` for MCP clients without their own file-write flow
