# Development

```bash
uv sync                                   # install
uv run hyperbox doctor                    # is this machine able to run sandboxes
uv run python -m hyperbox_mcp.server      # start the stdio server
```

Always go through `uv run`. A bare `python` is the system interpreter with
none of this project's dependencies.

## The rules that hold this together

**1. The Runtime boundary.** The lifecycle tools talk only to the
`Runtime` protocol in `runtime.py`. `llm_sandbox` may be imported *only*
in `llm_sandbox_runtime.py`. A `from llm_sandbox import ...` anywhere else
defeats the replaceable-backend design and is a bug, not a shortcut.

**2. Limits are server policy, never a caller's choice.** Memory, CPU,
PIDs, network posture, scratch size and the timeout ceiling live in
`policy.py`. Do not add a tool parameter that lets a caller raise any of
them.

**3. Descriptions are the routing logic.** An MCP client has no
dispatcher; it decides whether to call a tool from that tool's name,
description and annotations alone. This was measured: a working search
tool described only as "search the codebase" was refused by a client that
asked the user to upload files by hand; naming what it covered turned the
same tool into a correct answer. So the prose in `server.py` is
load-bearing. When you change a tool, say what it is for **and when not to
use it**, and keep the annotations honest.

**4. An entry in the language or backend map is a promise.** It says the
tool can deliver that environment. Nothing goes in until the full suite
passes for it against a real container. The same rule sets what may be a
*built-in* environment: only an image the project itself can pull. v0.2
briefly shipped `data-science` and `browser-testing` as built-ins with no
Dockerfile in the repo and, for one of them, no image anywhere — they
worked on the machine that had built them by hand and nowhere else.
Locally built environments are discovered from `~/.hyperbox/environments`
at runtime; they are the user's promise, not the project's.

**4b. What is resolved at import cannot change while the server runs.**
`hyperbox build` is a CLI subcommand and the server is a long-lived stdio
process, so anything the server learns about the filesystem at import is
frozen for days. Environments are resolved per call for exactly this
reason. Before assuming a module-level constant is fine, ask which
process writes it and which reads it.

**5. A slow operation must keep talking.** MCP clients kill a tool call
after a period of silence. Anything that can take longer than a few
seconds — creation pulls gigabytes on a cold machine — reports progress
while it works. Silence is indistinguishable from a hang, and the client
resolves that ambiguity by killing the request.

**6. A dropped connection is not an outage.** Engines close idle sockets;
a cached client keeps the dead one. Retry a connection-shaped failure
once against a fresh client, and never widen that to cover a genuinely
unreachable engine — the whole registry design rests on being able to
tell those apart.

**7. Validate at the boundary, act under the lock.** Every tool body runs
in one order: validate the inputs, take the per-sandbox lock, re-read the
record inside the lock, then act. A decision made on state read before the
lock is already stale.

## Testing

```bash
uv run python tests/verify_platform.py    # host side only, no container
uv run python tests/run_all.py docker     # every suite, one summary
uv run python tests/run_all.py podman     # second engine, also supported

# or individually
uv run python tests/verify.py             # lifecycle, validation, MCP surface
uv run python tests/verify_registry.py    # ownership and races across processes
uv run python tests/verify_limits.py      # enforced resource policy
uv run python tests/verify_security.py    # container configuration, structurally
uv run python tests/verify_containment.py # hostile code, contained
```

`run_all.py` also asserts the run left no HyperBox-labelled containers
behind.

**There are no mocked tests on the sandbox path, on purpose.** Acceptance
is defined against a real container, because mocking Docker would prove
only that the mock works. Two consequences worth knowing:

- The suites need an engine running, and take anywhere from 3 to 15
  minutes. That spread is not flakiness in the assertions — they pass
  consistently — but contention: several cases deliberately balloon to
  the 1 GB memory ceiling and get OOM-killed, and whichever suite runs
  while the engine's VM is reclaiming pays for it. Expect roughly 5
  minutes on an idle machine.
- When something fails, triage the layer before editing code. Is the
  engine running? Is the image pullable from here? `hyperbox doctor`
  answers both. A create failure is far more often the environment than
  the implementation.

The one place mocking would be tempting — proving that an unreachable
engine is handled correctly — is done instead by pointing a second server
process at a socket where nothing is listening. A real outage, no mock.

## Podman status

Supported. `tests/run_all.py podman` passes every suite against real
containers.

Getting there needed one non-obvious thing, kept in
`engine.ensure_podman_transport`: the client must talk to the machine's
**unix socket**, never the TCP port it forwards. Over the forward, exec
returns correct exit codes and zero bytes of output — verified against
the raw HTTP API, so it is below every client library. On macOS the
socket path also has to be found without relying on `$TMPDIR`, because
MCP clients launch servers without it and Podman then reports the wrong
path. Do not "simplify" that function back to `PodmanClient.from_env()`.

`auto` still prefers Docker, as a stable default rather than a judgement
about Podman.

## Promoting a language

`policy.LANGUAGES` and `_LANGUAGES` in `llm_sandbox_runtime.py` ship
`python` only. `tests/verify.py` keeps snippet sets for `javascript`,
`ruby` and `go` so each can be re-promoted:

1. Add the language to both maps.
2. Add a canary snippet to `_CANARY` in `llm_sandbox_runtime.py`.
3. `uv run python tests/run_all.py docker` and
   `uv run python tests/verify.py docker <language>` must both pass.
4. Only then commit the map change.

## Upgrading the execution backend

`llm-sandbox` is pinned to `>=0.3.44,<0.4` because HyperBox depends on
behaviour that is not part of its public contract:

- `runtime_configs` being forwarded verbatim into `containers.create()`
- `create_session(container_id=...)` reattaching to an existing container
- the exception layout (`SandboxTimeoutError` living in
  `llm_sandbox.exceptions`, not at the top level)
- the workdir ownership handling during environment setup

So a version bump is a deliberate act, not a routine one:

1. Change the pin in `pyproject.toml`, `uv sync`.
2. Run every suite on Docker, twice.
3. Re-read the notes at the top of `llm_sandbox_runtime.py` and confirm
   each assumption still holds.
4. Commit `uv.lock` with the pin change.

## Repository layout

```
README.md            what it is, why, install, limits
pyproject.toml       dependencies and the `hyperbox` entry point
src/hyperbox_mcp/
  server.py          the MCP surface: three tools, a resource, a prompt
  policy.py          every server-set limit, in one place
  validate.py        strict checks on caller input
  engine.py          the only module that talks to an engine client
  registry.py        durable ownership, shared across processes
  runtime.py         the Runtime protocol
  llm_sandbox_runtime.py   the one implementation of it
  filelock.py        cross-process locking, POSIX and Windows
  doctor.py, cli.py  terminal subcommands
  clientconfig.py    generates MCP client configuration
tests/               acceptance suites, all against real containers
docs/                architecture, security, troubleshooting, deployment
  client-examples/   per-client configuration, generated by `hyperbox config`
```

## Manual check before releasing

Automated suites cannot cover the client integration. Before calling a
build good:

1. Install from a fresh clone in a clean directory, and run
   `hyperbox doctor`.
2. Register the server in a real MCP client using the stdio config from
   the README.
3. Confirm the client discovers all three tools.
4. Drive the loop by hand: create → code that succeeds → code that fails
   → destroy.
5. Restart the client, then destroy a sandbox created before the restart.
   This is the cross-process ownership property, and it is the one most
   likely to break silently.
6. Confirm no containers are left:
   `docker ps -a --filter label=hyperbox-mcp.managed=true`.
