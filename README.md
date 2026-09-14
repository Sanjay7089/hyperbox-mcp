# HyperBox

**An MCP server that gives your coding agent a disposable, network-sealed
container to run generated code in.**

[![PyPI](https://img.shields.io/pypi/v/hyperbox-mcp.svg)](https://pypi.org/project/hyperbox-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/hyperbox-mcp.svg)](https://pypi.org/project/hyperbox-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[Documentation →](https://sanjay7089.github.io/hyperbox-mcp/)**

---

An agent writes code, then reports whether it works. Those are the same
party, and nothing independent sits in between. HyperBox is the thing in
between: the code runs in a container, and the agent gets back real
stdout, real stderr and a real exit code it did not author.

That container is also not your machine — no access to your files, your
credentials or your network.

## Install

Requires Python 3.11+ and either Docker or Podman.

```bash
pip install hyperbox-mcp          # or: uv tool install hyperbox-mcp
hyperbox doctor --pull            # check the machine, pre-pull the base image
hyperbox config --format json     # print a client config to paste
```

Merge that config into your MCP client and restart it — MCP servers are read
at launch. Per-client instructions, including Cursor, Antigravity and
Continue, are in [docs/setup.md](docs/setup.md).

## Tools

| Tool | Purpose |
|---|---|
| `create_sandbox(language, backend, environment, packages, sync_from)` | Creates a sealed container with any declared packages already installed. Returns a `sandbox_id`. |
| `run(sandbox_id, code, timeout)` | Executes code. Returns `stdout`, `stderr`, `exit_code`, `success`, `timed_out`, `language`. |
| `run(sandbox_id, code, background=True)` | Starts a long-running process — a server, a worker — and returns a `process_id`. |
| `get_process_logs(sandbox_id, process_id)` | Reads what a background run has printed so far. |
| `destroy_sandbox(sandbox_id)` | Tears it down. Idempotent, and confirmed against the engine before reporting success. |

Plus a `hyperbox://capabilities` resource listing the real limits, so an agent
can read them rather than discover them by failing, and a `run_safely` prompt
describing the verification workflow.

**Languages:** `python`, `javascript`, `bash`, `go`, `java` — each on an
official tagged image.

## Dependencies

Declare them at creation. They install while the sandbox can still reach a
package index; the network is then detached for good.

```python
create_sandbox(language="python", packages=["requests", "pandas==2.2.0"])
```

Version pins use each language's own syntax — `pandas==2.2.0`,
`mime-db@1.54.0`, `github.com/spf13/cobra@v1.8.0`.

`run(libraries=[...])` still works but reopens the network on a sealed
sandbox, returns a `deprecation` field, and will be refused in a future
release.

Within one sandbox the filesystem and installed packages persist across runs.
Variables do not — each `run` is a fresh process, so write anything you need
to keep to a file.

## Running a real project

`create_sandbox(sync_from="…")` copies a directory into the sandbox so an
agent can run your actual test suite. It is off until you enable it once:

```bash
cd ~/code/my-project && hyperbox init
```

That gate is a terminal command on purpose — an agent cannot widen its own
boundary. Credential files (`.env`, `id_rsa`, `.netrc`, `.aws/` and others)
are never copied, and every exclusion is reported in the result.

The copy is taken once, at creation. It is not a live mount.

## Custom environments

Start from a heavier image instead of paying an install on every sandbox:

```bash
hyperbox build data-science --dockerfile ./Dockerfile
hyperbox build torch --image pytorch/pytorch:latest
hyperbox envs
```

Agents then request it by name: `create_sandbox(environment="data-science")`.
Building is a CLI action on purpose — an agent may use an environment but
cannot create one.

## What is enforced

1 GB memory, 1 CPU, 128 processes, a 60-second ceiling per run, capped
output. These live in `policy.py` and no tool parameter can raise them. After
creation the container's real configuration is read back off the engine and
compared — a mismatch destroys it rather than handing back a sandbox that is
only *described* as limited.

The containment suite runs genuinely dangerous code against a real container:

```
$ python tests/verify_containment.py docker

PASS  a host path is unreachable from inside
PASS  the user database inside is the container's, not the host's
PASS  network is unreachable from inside
PASS  container engine socket is not mounted
PASS  memory exhaustion is capped, with a legible reason
PASS  process explosion is capped by the PID limit
PASS  sandbox is destroyed cleanly afterwards

7/7 contained
```

The fork bomb stops at 126 processes against a ceiling of 128.

## What it does not do

- **Containers share your host's kernel.** This is developer containment, not
  a VM, microVM or gVisor boundary.
- **Not a multi-tenant boundary.** Do not run untrusted third-party code as a
  service with it.
- **Code in the default images runs as root inside the container.** An
  environment built from an image that declares a `USER` runs as that user
  instead; see [docs/security.md](docs/security.md).
- **Packages come from public indexes** and are not vetted.

If you need a boundary against genuinely adversarial code, you want a VM or
microVM sandbox rather than a local container.

## Documentation

- [Setup](docs/setup.md) — install, client configuration, environments, CLI
- [Architecture](docs/architecture.md) — the layers and why each boundary sits where it does
- [Security model](docs/security.md) — what is enforced, how it is proven, what it does not cover
- [Agent teams](docs/agent-teams.md) — several agents at once, and what holds under them
- [Troubleshooting](docs/troubleshooting.md)
- [Changelog](CHANGELOG.md) · [Security policy](SECURITY.md) · [Contributing](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
