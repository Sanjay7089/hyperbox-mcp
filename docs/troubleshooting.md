# Troubleshooting

Start here:

```bash
hyperbox doctor
```

It checks the interpreter, both container engines, which one `auto`
resolves to, whether the sandbox image is present, whether the registry
is readable, and finally creates a real sandbox, runs code in it, and
destroys it. Every failing line names its fix. Exit code is 0 only if
everything passed.

Add `--pull` to fetch the sandbox image ahead of time, or `--quick` to
skip the live round trip.

---

## doctor says "docker reachable" but Docker is not installed

Podman serves a Docker-compatible endpoint. On Windows it commonly owns
`npipe:////./pipe/docker_engine`, so a Docker client connects happily to
what is really Podman.

HyperBox now identifies the engine from the API rather than from the pipe
that answered, so `doctor` reports which product is running:

```
PASS  docker endpoint reachable — served by podman
        product: podman
        note: this endpoint is served by podman, not docker
```

`auto` resolves to the real engine, so a sandbox created on a
Podman-only machine reports `backend: "podman"`. Asking explicitly for
`backend="docker"` there is refused with a message naming what actually
answered, rather than succeeding under the wrong name.

## `backend="podman"` fails while `backend="auto"` works

Fixed. HyperBox used to try a fixed list of Podman pipe names, none of
which matched a Podman that had taken over `docker_engine`. It now asks
Podman where it listens (`podman machine inspect`,
`podman system connection list`) and, failing that, tries the
Docker-compatible pipe — accepting it only if the engine on the other
end really is Podman.

If it still fails, `hyperbox doctor` prints every pipe that was tried and
what answered on each.

## "The pipe is being closed" / "Remote end closed connection"

Container engines close idle connections, and Docker Desktop on Windows
does it within seconds on a named pipe. A cached client keeps the dead
handle, so the *next* call fails while the engine is perfectly healthy.

Every engine call now retries once against a freshly built client when
the failure looks like a dropped connection — including the Windows
phrasings (`WinError 232`, `WinError 109`, "pipe is being closed", "pipe
has been ended"). A genuinely unreachable engine still fails twice and is
reported as unreachable, which is the distinction the registry depends on:
`destroy_sandbox` must never claim a cleanup it could not verify.

If you saw containers left behind by an earlier version:

```
docker ps -a --filter label=hyperbox-mcp.managed=true
```

They are reclaimed automatically the next time a server starts.

## "No container engine is reachable"

Nothing is running that can host a container.

- **Docker**: start Docker Desktop, or `sudo systemctl start docker` on
  Linux. Confirm with `docker info`.
- **Podman**: `podman machine start` (first time: `podman machine init`).

`hyperbox doctor` prints each engine's own diagnosis, so you do not have
to guess which one it was unhappy about.

## Docker is running but HyperBox cannot see it

Usually the socket is not where the client looks.

```bash
docker context ls        # which context is active, and its socket
echo "$DOCKER_HOST"      # if set, it wins over the context
```

If your MCP client launches HyperBox with a trimmed environment, it may
not inherit `DOCKER_HOST`. Set it explicitly in the client's `env` block.

## "connection refused" from Podman, but Podman is running

Fixed in 0.2.2. A stopped `podman machine` leaves its socket **file** on
disk:

```bash
ls -l /var/folders/*/*/T/podman/*-api.sock   # still there after `machine stop`
```

HyperBox used to accept any socket path that existed, so it pointed
`CONTAINER_HOST` at a socket nothing was listening on and every call came
back `connection refused`. Two things turned that into a permanent
condition rather than a passing one:

- the resolved socket was never re-checked, so a server process that
  latched onto a dead path kept it for its whole life — and a server inside
  an editor runs for days, including across a `podman machine restart` that
  fixed the underlying problem;
- dropping the cached client left the dead path in place, so the retry
  reconnected to the same socket.

HyperBox now connects to a candidate before trusting it, re-resolves a
setting that has gone dead, and drops the resolved socket whenever it drops
its clients. Restarting the Podman machine no longer needs the MCP client
restarted with it.

If you still see it, `hyperbox doctor` names the socket actually in use.

## A run timed out — did the code stop?

Yes, since 0.2.2. Before that it did not: the timeout returned
`timed_out: true` while the code kept running at the sandbox's full CPU
limit until the sandbox was destroyed.

The result now says which happened:

- *"The code was killed; the sandbox is still usable."* — the normal case.
  Files, including `/work`, are untouched.
- *"The sandbox was restarted…"* — the fallback, when something survived.
  `/work` is tmpfs, so it comes back empty; installed packages and the rest
  of the filesystem survive. The sandbox is re-sealed and checked before
  being handed back.
- A warning naming what could not be done, if neither worked. Call
  `destroy_sandbox` in that case.

## Testing an unreleased checkout in a client

`hyperbox config` resolves `hyperbox` through PATH, which is right for an
installed release and wrong for a branch you are testing: if a release is
already installed, the generated config launches **that**, your checkout
never runs, and nothing about the config looks wrong.

```bash
hyperbox config --local --format json
```

`--local` pins the executable in the environment you ran it from. The
config is then tied to that directory and breaks if you move it, which is
the intended trade.

## Podman is installed but nothing finds it

Two separate problems, often confused.

**The CLI is not on PATH.** The macOS installer puts it at
`/opt/podman/bin/podman`, which is not on a default PATH. HyperBox looks
there and in the Homebrew locations, so `hyperbox doctor` reports the
real path rather than claiming Podman is missing. The API connection does
not use the CLI at all, so Podman can be perfectly usable while `which
podman` finds nothing.

**The socket cannot be found.** HyperBox sets `CONTAINER_HOST` itself,
preferring the machine's unix socket over the TCP forward that Podman's
own connection config advertises — see the next section for why that
matters. To override it by hand:

```bash
export CONTAINER_HOST="unix://$(podman machine inspect \
  --format '{{.ConnectionInfo.PodmanSocket.Path}}')"
```

## "output does not reach this server" on Podman

```
The podman backend started a container but its output does not reach
this server ... Every run in this sandbox would report success with
empty output, so it is refused rather than handed back.
```

This means the Podman client connected over the **forwarded TCP port**
instead of the machine's **unix socket**.

A `podman machine` publishes both, and `PodmanClient.from_env()` prefers
the TCP forward. Over it, containers create and start, inspect works, and
exit codes are correct — but every exec returns zero bytes. That was
confirmed against the raw HTTP API with no client library involved:
`/exec/{id}/start` returns `200` with an empty body while the exit code
comes back fine, and the identical exec over the unix socket returns a
properly framed stdout stream.

HyperBox resolves the socket itself before building a Podman client, so
this should not happen. If it does, set the transport explicitly:

```bash
export CONTAINER_HOST="unix://$(podman machine inspect \
  --format '{{.ConnectionInfo.PodmanSocket.Path}}')"
```

**One caveat on macOS:** Podman derives that path from `$TMPDIR`. A
process launched without `TMPDIR` — which is how MCP clients launch their
servers — is told the socket is at `/tmp/podman/...` when it is really
under `/var/folders/.../T/podman/`. HyperBox handles this by searching
both locations, so `hyperbox doctor` is the quickest way to see which
socket is actually in use.

## "The container engine did not apply this server's resource policy"

The engine accepted the configuration and applied something else. The
message names the field and what the container actually reports.

This is a guard, not a bug in your setup: a container that is not
actually limited must never be handed back and described as limited. If
you hit it on a normal Docker install, please report it with the full
message — it means that engine records limits somewhere new.

## "destroy against an unreachable engine returns an error"

Deliberate. If the engine cannot be reached, the container has not been
proven gone, so HyperBox reports the error and **keeps the sandbox on
file** rather than forgetting a container that may still be running.

Start the engine and call `destroy_sandbox` again, or let the inactivity
timeout reclaim it once the engine is back.

## A sandbox disappeared between calls

Sandboxes are reclaimed after a period of inactivity (30 minutes by
default). An actively used sandbox is never reclaimed — the timer resets
on every `run`. To change it:

```bash
export HYPERBOX_TTL_SECONDS=7200
```

## Where is the state kept?

Outside the repository, so a checkout is not polluted and two worktrees
cannot clobber each other's registries:

```
$HYPERBOX_STATE_DIR                       if set
~/.hyperbox/state                         otherwise
```

`hyperbox doctor` prints the path in use and lists any sandboxes on file,
flagging rows whose container no longer exists.

Everything HyperBox owns sits under one directory:

```
~/.hyperbox/state           the SQLite registry and per-sandbox locks
~/.hyperbox/logs            server.log, rotated at 5 MB, 3 kept
~/.hyperbox/environments    one directory per custom environment
```

Upgrading from v0.1 moves the registry across from
`~/.local/state/hyperbox-mcp/` the first time a server starts. It never
overwrites a registry already at the new path, moves only `registry.db`
and its WAL siblings, and leaves the old directory in place.

## What did the server actually do?

A stdio server cannot print — stdout is the JSON-RPC channel the client
is reading — so there is a log instead:

```bash
hyperbox logs             # print it
hyperbox logs --follow    # and keep printing
```

It records sandbox creation, every run with its exit code, destruction
and garbage collection. This is the thing to read when a client reports a
failure with no detail: the client shows you the tool result, the log
shows you what the server was doing at the time.

Nothing is logged at import, only from a running server, so
`hyperbox doctor` and `hyperbox config` never write to it.

## "Unknown environment"

`create_sandbox(environment=...)` only accepts environments that already
exist. List them:

```bash
hyperbox envs
```

If the one you want is missing, build it — an agent cannot, by design:

```bash
hyperbox build my-env --custom ./Dockerfile
```

A running server picks up the new environment without a restart; the map
is rescanned on every call. If the name is rejected outright rather than
reported as unknown, it failed the name check: lowercase letters, digits,
dots, hyphens and underscores, starting with a letter or digit.

## Containers left behind

There should not be any. If you want to check:

```bash
docker ps -a --filter label=hyperbox-mcp.managed=true
```

Every HyperBox container carries `hyperbox-mcp.managed` and
`hyperbox-mcp.id`. Garbage collection matches on those labels and only
those, so a container HyperBox did not create is never touched. Stray
ones are reclaimed the next time a server starts.

## "Tool call failed: request timed out after 60000ms"

The first `create_sandbox` on a machine pulls the language image, which
is several gigabytes. MCP clients cut a tool call off after a period of
silence — 60 seconds is typical — so the pull used to be killed partway
through with no explanation.

The server now reports progress every few seconds for the whole of
creation, so the client sees activity well inside its timeout and a cold
start completes. If you still hit this, the pull is genuinely stalled
rather than slow: check the engine can reach `ghcr.io`, or pre-pull with
`hyperbox doctor --pull` and watch it directly.

## "Remote end closed connection without response" on destroy

Container engines close idle connections — Docker Desktop on Windows does
it within seconds on its named pipe. HyperBox caches its engine client,
so an operation after a pause could inherit a socket the engine had
already closed, and a healthy engine looked unreachable.

That mattered more than a spurious error: `destroy_sandbox` refuses to
confirm removal when the engine is unreachable, so it left the container
running with its registry row intact. A connection-shaped failure is now
retried once against a fresh client, while a genuinely unreachable engine
still fails both times and is reported honestly.

If you have orphans from before this fix:

```bash
docker ps -a --filter label=hyperbox-mcp.managed=true
```

They are reclaimed automatically the next time a server starts.

## `hyperbox doctor` shows rows "still being created"

A create that died between reserving its id and finishing — a killed
server, a stalled pull — leaves a `creating` row. Those are deliberately
excluded from the normal expiry sweep, because a reservation is young by
definition and its container may not exist yet.

They are now cleared by garbage collection once past their TTL, so they
no longer accumulate. Starting a server runs that sweep.

## The first sandbox takes a long time

It pulls the language image (`ghcr.io/vndee/sandbox-python-311-bullseye`),
which is large. Later sandboxes reuse it. Pre-pull with
`hyperbox doctor --pull`.

## `run` returns nothing useful

Remember what persists between calls in one sandbox:

- the filesystem, including `/work` — **yes**
- packages installed via `libraries` — **yes**
- variables from a previous `run` — **no**

Each run is a fresh process. Write anything you need to keep to a file
under `/work`.

## A library will not install

Only plain package names are accepted: `requests`, `pandas==2.2.0`,
`uvicorn[standard]`. Installer flags, URLs, filesystem paths and VCS
references are refused on purpose — that install is the only moment the
sandbox can reach the network. See [security.md](security.md).
