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
$XDG_STATE_HOME/hyperbox-mcp              if set
~/.local/state/hyperbox-mcp               otherwise
```

`hyperbox doctor` prints the path in use and lists any sandboxes on file,
flagging rows whose container no longer exists.

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
sandbox can reach the network. See [security-model.md](security-model.md).
