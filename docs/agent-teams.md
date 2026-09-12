# Agent teams and subagents

HyperBox is built for more than one agent at a time. Not as a feature
that was added, but because the failures that come with several agents —
a restarted client losing track of a container, four servers saturating
one engine, two windows fighting over one sandbox — are the ones that
shaped the design.

This page is what that means in practice.

## A worked example

A planner splits a refactor across three subagents: one migrating a data
layer, one updating tests, one checking a dependency bump.

**Each takes its own sandbox.**

```
subagent A → create_sandbox(language="python", sync_from="~/proj/db")
subagent B → create_sandbox(language="python", sync_from="~/proj/tests")
subagent C → create_sandbox(language="python", packages=["requests"])
```

Those three creates **queue**. Creating a sandbox may pull gigabytes, and
three at once saturates the engine and starts failing calls for reasons
that have nothing to do with the caller. So creates, pulls and builds
take a slot from a bounded pool — and the pool is a set of lock *files*,
not an in-process semaphore, so the bound holds across every HyperBox
process on the machine. Two editors and a terminal agent share it.

Then their `run` calls **do not queue**. Executing code is a lightweight
call on a connection that already exists; the contention is at connection
setup, not execution. Putting a lock on the hot path would add latency to
solve a problem it does not have. So once the sandboxes exist, the three
subagents run in parallel.

**When A finishes, it destroys its sandbox** — and if it does not, the
sandbox is reclaimed after an inactivity timeout, so an agent that
crashes mid-task does not leak a container.

## The rule: one sandbox per subagent

Give each subagent its own sandbox rather than sharing one.

Sharing is *supported* — any agent with the id can use it — but calls
against a single sandbox are **serialised**, because two processes
driving one container concurrently is not something a container engine
makes safe. Two subagents sharing a sandbox will wait on each other.

Sharing also leaks state. A sandbox's filesystem and installed packages
persist between runs, which is exactly what you want within one task and
exactly what you do not want across two. Subagent B's test fixtures
should not be visible to subagent C.

The cost of a sandbox is one container. That is cheaper than the
debugging that follows from sharing one.

## Ownership survives restarts

This is the property most likely to matter and least likely to be
noticed until it breaks.

**Ownership does not live in the server process.** It lives in a SQLite
registry outside the repo, shared by every HyperBox process. So:

- A client that restarts mid-task can still destroy a sandbox created
  before the restart.
- A second client window can destroy a sandbox the first one made.
- A subagent replaced by a fresh one can clean up its predecessor's work.

Without this, a restart orphans containers — and the client would report
success while doing nothing, because it no longer knows what it owned.

## Handing work between agents

A sandbox id is the handle. Passing that string is enough:

```
planner  → create_sandbox(...) → "a3f2c1d0e9b8"
worker A → run(sandbox_id="a3f2c1d0e9b8", ...)
worker B → run(sandbox_id="a3f2c1d0e9b8", ...)   # serialised behind A
planner  → destroy_sandbox("a3f2c1d0e9b8")
```

Any process can act on any sandbox it has the id for. There is no session
to keep alive and no affinity to a particular server.

For results, prefer the filesystem over the conversation: write to
`/sandbox` or `/work` and have the next agent read it, rather than
passing large output through the model. A human can retrieve it with
`hyperbox pull`.

## Long-running services between agents

One subagent can start a server and another can test against it, in the
same sandbox:

```
subagent A → run(background=True, code="...serve on 127.0.0.1:8000...")
          → {"process_id": "…", "status": "running"}
subagent B → run(code="...urllib.request.urlopen('http://127.0.0.1:8000')...")
```

Loopback works even though the sandbox has no route to the internet:
sealing detaches the sandbox's networks, and `lo` remains. Reading the
background process's logs with `get_process_logs` counts as using the
sandbox, so a polled service does not expire underneath the agents
watching it.

**That is the only way to reach it.** No port is published to the host,
so the service is invisible from your browser, your terminal, and any
other program on your machine — `curl http://localhost:8000` on the host
will not reach a sandbox listening on 8000. An agent that reports a URL
to a user is reporting something that does not resolve. Verify a service
the way subagent B does above: request it from inside the same sandbox
and report the response.

## Seeing what your agents are doing

Agents create sandboxes; you can inspect and clean up without one:

```bash
hyperbox ps                  # everything on file, with idle time and TTL
hyperbox rm <id>             # destroy one
hyperbox logs --follow       # what the server is doing
```

`hyperbox ps` reads the registry rather than the engine, deliberately —
a divergence between what HyperBox believes it owns and what is actually
running is exactly what you want to see.

## Tuning for a team

| | |
|---|---|
| `HYPERBOX_ENGINE_SLOTS` | How many heavy engine operations run at once, across every process. Default `min(4, cpu_count)`. Lower it on a shared or small machine. |
| `HYPERBOX_TTL_SECONDS` | How long an idle sandbox survives (default 1800). Raise it if agents leave sandboxes idle between phases; lower it if they leak. |

Every sandbox holds its own 1 CPU and 1 GB ceiling. Ten concurrent
subagents is ten containers — the per-sandbox limits bound each one, not
the total, so size the fleet to the machine.

## What this does not do

- **It is not a scheduler.** HyperBox bounds engine work and serialises
  per sandbox; deciding which subagent runs when is your orchestrator's
  job.
- **It is not a multi-tenant boundary.** Subagents in one HyperBox
  installation are all *your* agents. Do not use it to separate one
  customer's code from another's — see the
  [security model](security.md).
- **Sandboxes do not share a network.** Each is sealed, so two sandboxes
  cannot talk to each other. Services shared between agents live inside
  one sandbox, on loopback.
