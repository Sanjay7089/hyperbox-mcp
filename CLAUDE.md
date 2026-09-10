# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

HyperBox is a stdio MCP server that gives an LLM agent a disposable
Docker/Podman container to run code in, so generated code executes away
from the host filesystem, credentials and network. Four tools only:
`create_sandbox`, `run`, `get_process_logs`, `destroy_sandbox`, plus a
`hyperbox://capabilities` resource and a `run_safely` prompt. Nothing
else is on the tool surface.

## v0.4: NativeRuntime only

`native_runtime.py` is now the **only** `Runtime` implementation — pure
REST calls (`rest/`) onto the Docker/Podman engine API, no execution-SDK
underneath. `llm-sandbox` and the SDK-based runtime it required are
removed. `HYPERBOX_RUNTIME` is still read, but any value other than
`"native"` is refused with an explanation, not silently ignored.

The `Runtime` protocol (`runtime.py`) is still the boundary everything
else talks to — the point is a future backend can still be swapped in by
satisfying that protocol — but do not assume a second implementation
exists today. Docstrings in `runtime.py` and docs under `internal/` still
describe the old `llm_sandbox_runtime.py`; treat those as historical, not
current.

## Threat model

- The agent's *generated code* is the untrusted party, not the MCP
  client or the human. The server exists to contain what that code can
  reach: host filesystem, host network, the container engine socket.
- **Not a multi-tenant or adversarial-service boundary.** Containers
  share the host kernel — this is developer containment (accidental
  `rm -rf`, a runaway install, a script that phones production), not a
  gVisor/Firecracker-grade isolation guarantee against a determined
  attacker.
- Code runs as root inside the container (a non-root user broke the
  execution backend; documented as a known gap, not silently fixed).
- A security property is not real until it's read back from the engine
  (see Resource policy) — an accepted config is not a proof of an
  applied one.

## Preparation vs execution boundary

Two distinct phases with different trust levels, split at
`create_sandbox`:

- **Preparation** (`packages=[...]` at creation): network is open, so
  declared dependencies can install, then the network is sealed for
  good. This is the only window untrusted install scripts get network
  access, and it closes before any agent-submitted code runs.
- **Execution** (`run`): network posture is whatever the sandbox was
  sealed to; no run-time parameter can reopen it. `run(libraries=[...])`
  still works for back-compat but must reopen a sealed network to do so,
  returns a `deprecation` field, and should not be the normal path —
  prefer `packages` at creation.

## Sandbox persistence semantics

Within one sandbox: filesystem and installed packages persist across
`run` calls. Variables do not — each `run` is a fresh process. Anything
that must survive between runs goes on disk (`/work`), not in memory.

A sandbox outlives the server process that created it: ownership lives in
the registry (SQLite, outside the repo), not process memory, so a
restarted or second server process can still find and destroy a sandbox
it didn't create, by reattaching to the container id in the record.

## Resource policy

All limits (memory, CPU, PID count, timeout ceiling, output size caps,
scratch size, network posture, the language/backend/environment maps)
live in exactly one place: `policy.py`. No tool parameter may raise any
of them. After creation, the real container's configuration is read back
from the engine and compared to policy — a mismatch destroys the
container and raises, rather than handing back a sandbox that is
*described* as limited while the engine silently ignored the setting.

## Network policy

Network posture is a property of the environment, not a caller choice:
built-in and default environments are sealed after preparation; only an
environment a human built with `--allow-network` (`policy.
environment_allows_network`, read per call from its manifest) keeps
network for its entire lifetime. No `create_sandbox`/`run` parameter can
request network access directly.

## Registry vs engine semantics

- **Registry** (`registry.py`): the source of truth for *ownership and
  state* — which sandbox_ids exist, in what state (`creating` → `ready`),
  and when they expire. It is durable and outside process memory
  specifically so a restart doesn't lose track of a live container.
- **Engine** (`engine.py`, `rest/`): the source of truth for *what is
  actually running*. `engine.py` is the only module permitted to hold a
  real engine client, and it must distinguish "engine answered, container
  gone" (`ContainerGoneError`) from "engine could not be asked at all"
  (`EngineUnavailableError`) — collapsing those is how orphans and false
  "already cleaned up" reports happen.
- Every container carries `hyperbox-mcp.managed` / `hyperbox-mcp.id`
  labels; both directions of GC key off them, never off registry state
  alone.

## Cross-process locking

Every state change takes a per-sandbox `flock` (`filelock.py`) and
**re-reads the record inside the lock** before acting — a decision made
on a record read before the lock is already stale. The lock is an OS
file lock, so a killed server releases it automatically; a crashed
process must never wedge a sandbox permanently.

A registry row is written in state `creating` *before* the container
exists. GC ignores `creating` rows outright, so nothing can reclaim a
container in the window between the engine creating it and the server
recording it.

## Two-way GC

Garbage collection runs both directions, because either side of the
registry/engine split can be missing what the other has:

1. **Registry → engine** (`server.collect_garbage`): for each expired,
   `ready` record, re-check expiry under that sandbox's lock, destroy the
   container, then drop the row. A `run` racing GC in another process
   revives the record before the lock is taken, so it survives.
2. **Engine → registry** (`sandbox_ops.collect_orphans`): sweep
   containers carrying HyperBox's labels that the registry does *not*
   know about, and remove them. Containers younger than
   `policy.GC_GRACE_SECONDS` are skipped — another process may be mid-way
   through creating and registering one.

Both directions are best-effort per engine: an engine that's absent or
unreachable is skipped, not fatal — GC must never stop the server from
serving.

## Timeout semantics

`run`'s timeout is capped at `policy.MAX_TIMEOUT_SECONDS` (60s) and
enforced by killing the container's process **group**, with the kill
verified rather than assumed, so anything the code forked dies with it.
The engine socket's own timeout must exceed the execution deadline by a
real margin (currently the deadline + 120s) — making them equal or close
turns a normal timeout into a coin-flip between a clean `timed_out: true`
and a raw, misleading `TimeoutError` from the transport. Never let a
socket-level timeout collide with `MAX_TIMEOUT_SECONDS`.

## Testing philosophy

No mocks on the sandbox path, on purpose — mocking Docker/Podman proves
only that the mock works. Acceptance suites run against a real container
and are the actual proof of containment, resource limits and lifecycle
correctness; CI's host-only suite is a smaller, faster complement, not a
replacement. The one place mocking would be tempting — an unreachable
engine — is instead tested by pointing a real server at a socket with
nothing listening, i.e. a real outage.

See the `test` skill (or `internal/development.md`) for how to run and
choose among suites — that's a procedure, not a fact to keep here.

## Non-goals

- Not a hard security boundary for genuinely adversarial/untrusted
  third-party code as a multi-tenant service — see Threat model.
- No gVisor/Firecracker/VM-level isolation is provided by HyperBox
  itself; containers share the host kernel.
- Building an environment (`hyperbox build`) is deliberately **not** an
  agent-facing tool — it runs an unsandboxed `docker build` (root,
  network, no policy caps). Agents may only *select* an existing
  environment, never create one.
- Dependencies installed via `packages`/`libraries` come from the public
  package index and are not vetted by HyperBox.
- No tool parameter is ever allowed to raise a policy limit or reopen a
  sealed network — if a change needs that, the change is wrong, not the
  policy.
