# Architecture

HyperBox is a stdio MCP server. It exposes three tools that create,
use and destroy a container, and nothing else. This document explains
how the pieces fit and why the boundaries sit where they do.

```
MCP client (any: an editor, a desktop app, a CLI agent)
        │ stdio, JSON-RPC
        ▼
HyperBox server  (server.py, FastMCP)
   ├── create_sandbox / run / destroy_sandbox
   ├── hyperbox://capabilities   discoverable limits
   ├── run_safely                prompt
   │
   ├── policy.py    the limits, in one place, set by the server
   ├── validate.py  every caller-supplied value, checked before use
   ├── registry.py  SQLite ownership, shared across processes
   ├── engine.py    the only module that talks to a container client
   │
   │        talks only to the Runtime protocol
   ▼
Runtime  (runtime.py)  ◄── LLMSandboxRuntime (llm_sandbox_runtime.py)
                                    │
                                    ▼  Docker or Podman container
```

## The Runtime boundary

Everything above `runtime.py` talks to a `Runtime` protocol —
`create` / `run` / `destroy` / `alive` / `gc` — never to an execution
backend directly. `llm-sandbox` is one implementation, living in
`llm_sandbox_runtime.py`, which is the only file permitted to import it.

The point is replaceability. If that dependency is abandoned, becomes
limiting, or a microVM backend becomes worthwhile, a new class satisfying
the same protocol replaces it and one line of wiring changes. The MCP
layer never notices. A `from llm_sandbox import ...` anywhere else is a
bug, not a shortcut.

`engine.py` is a separate concern and deliberately not behind that
boundary: it speaks to the Docker and Podman *client libraries* for
things a code-execution abstraction does not cover — asking whether an
engine is reachable, reading a container's real configuration back,
finding our own containers by label.

## Limits are server policy

Memory, CPU, process count, the timeout ceiling, output caps, network
posture and scratch-space size all live in `policy.py`. No tool takes a
parameter that raises any of them. This is the project's central
constraint: the model proposes what to run, the server decides the
ceiling it runs under.

After a container is created its configuration is read back off the
engine and compared to policy. A mismatch destroys the container and
raises. An engine that accepts a configuration and silently applies none
of it would otherwise hand back a sandbox that gets *described* to an
agent as limited while being nothing of the sort.

## Sandboxes outlive the process that created them

Ownership lives in a SQLite registry outside the repository (see
`registry.py` for the location), and every container carries
`hyperbox-mcp.managed` and `hyperbox-mcp.id` labels.

This matters because an MCP client restarts its servers freely, and may
run more than one at a time. With ownership in process memory, a restart
lost the sandbox while the container kept running — measured at 5 failures
in 10 when alternating calls between two live server processes, plus
orphaned containers that `destroy_sandbox` cheerfully reported as already
gone. A new process now reattaches by container id.

Two invariants keep the lifecycle race-safe:

1. **A row is written before its container exists**, in state `creating`.
   Garbage collection ignores those rows, so a container cannot be
   reclaimed in the window between the engine creating it and the server
   recording it.
2. **Every state change happens under a per-sandbox lock, and re-reads
   the record inside it.** A decision made on a record read before the
   lock was taken is already stale. Garbage collection re-checks expiry
   under the lock specifically so a sandbox someone is running code in
   cannot be collected out from under them.

The lock is a `flock` on a per-sandbox file, so the OS releases it if a
server is killed: a crashed process must not wedge a sandbox forever.

## Truthful failure

The registry exists to prevent orphaned containers, so the code must
never confuse these two situations:

- the engine answered, and the container is not there → success
- the engine could not be asked at all → an error, and nothing is forgotten

`engine.py` separates them (`ContainerGoneError` versus
`EngineUnavailableError`) and no function swallows a connection failure.
`destroy_sandbox` returns an error and *keeps* the registry row when the
engine is unreachable, because a container that cannot be reached has not
been proven gone.

## Descriptions are the routing logic

An MCP client has no dispatcher. It decides whether to call a tool purely
from the tool's name, description and annotations. This was measured: a
working search tool described only as "search the codebase" was refused by
a client that then asked the user to upload files by hand; naming what it
covered turned the same tool into a correct answer.

So the prose in `server.py` is load-bearing. Each tool says what it is
for **and when not to use it**, the annotations are honest (`run` is
marked not host-destructive; `destroy_sandbox` destructive but
idempotent), and a `hyperbox://capabilities` resource publishes the limits
so an agent can discover them without first crashing into them.

## How a client should use it

One sandbox per task, many runs, destroyed when the task finishes:

```
create_sandbox()  →  run()  →  run()  →  run()  →  destroy_sandbox()
```

Anything abandoned is reclaimed by the inactivity TTL, so a client that
crashes mid-task does not leak a container.

**Do not share one sandbox across unrelated tasks.** Inside a sandbox the
filesystem, installed packages and `/work` are shared, so two tasks in one
sandbox see each other's files and dependencies. That is useful within a
task and a liability across them.

Each stdio MCP client launches its own server process — a stdio server is
one connection, not a shared service — so two editors open at once means
two processes. That is safe because ownership lives in the registry rather
than in either process's memory: they can create separate sandboxes
without interfering, and either can destroy a sandbox the other created.
Both properties are asserted in `tests/verify_registry.py`.

## What "persistent" means

Verified against a real container, and narrower than it first appears:

- **The container persists** across `run` calls within one sandbox.
- **The filesystem persists**, including anything written to `/work`.
- **Installed packages persist** into later runs that do not re-declare
  them.
- **Interpreter memory does not.** Each `run` executes as its own process
  (differing PIDs, identical hostname), because each snippet is written to
  its own file and executed. A variable set in one call is gone by the
  next; write anything you need to keep to a file.

## Network posture

The container starts on its normal network and is immediately detached,
before any caller-supplied code runs.

Creating it with no network at all is not usable: `network_disabled=True`
gives the container no network sandbox, and the engine then refuses to
attach one later (404, "network sandbox not found"); `network_mode="none"`
fails the same way with a 400 on connect. Both were tried against a real
container. Detaching after start is what allows a network to be
re-attached for a dependency install and detached again afterwards.

This leaves a brief window between container start and sealing in which a
network exists. Only the backend's own environment setup runs in it —
nothing an agent submitted is ever executed unsealed.
