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

**The socket cannot be found.** `podman-py` reads `CONTAINER_HOST` first
and otherwise falls back to Podman's own connection config, so a missing
`CONTAINER_HOST` is often fine. When it is not:

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

This is Podman being **experimental**, working as intended.

On the Podman versions tested, `podman-py`'s `exec_run` returns no output
at all — with demux on or off, streaming or not — and its streaming exit
code is `None`, which becomes `0`. A sandbox built on it would answer
every run with "success, no output", which is worse than failing: an
agent would conclude its code printed nothing and start debugging code
that was fine.

So HyperBox runs a marker through the container at creation and refuses
the sandbox if it does not come back. **Use `backend="docker"`, or
`"auto"`, which prefers Docker.**

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
