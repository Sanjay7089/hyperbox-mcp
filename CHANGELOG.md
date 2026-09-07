# Changelog

All notable changes to HyperBox are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — breaking, with a migration window

- **Tool errors are now structured.** A failing tool call returns
  `{"error": {"code", "message", "fix", "context"}}` instead of
  `{"error": "some string"}`. The caller is usually a model that will read
  the error and retry, and one that says only what broke costs a round trip
  to learn nothing — so every error now carries a code to branch on and,
  where one exists, the command that fixes it.

  For one release the old shape is also present as a top-level
  `error_message` string, so a client parsing that keeps working. It will be
  removed in 0.4.0.

  Codes are stable and part of the public surface:
  `ENGINE_NOT_RUNNING`, `NO_ENGINE_INSTALLED`, `SOCKET_BUSY`,
  `CONTAINER_GONE`, `PROVISION_FAILED`, `EXECUTION_TIMEOUT`, `OOM_KILLED`,
  `NETWORK_LEAK`, `POLICY_NOT_APPLIED`, `SANDBOX_STALE`, `SANDBOX_FAILED`,
  `INVALID_INPUT`, `UNSUPPORTED_LANGUAGE`, `UNSUPPORTED_BACKEND`,
  `UNKNOWN_ENVIRONMENT`.

### Internal

- Container invariants shared by every runtime — the resource-policy
  read-back, network sealing, the OOM explanation, orphan collection —
  moved into `sandbox_ops.py` so a second execution backend shares the
  behaviour rather than a description of it.

## [0.2.2] — 2026-09-08

Three defects found in production use across multiple client windows. All
three were the same shape: the server reported success while doing nothing.

### Fixed

- **"connection refused to podman" on a machine whose Podman was healthy.**
  A stopped `podman machine` leaves its `*-api.sock` file on disk, and the
  resolver accepted any path that existed, so `CONTAINER_HOST` was set to a
  socket nothing was listening on. Two things made it permanent rather than
  transient: the resolver returned early whenever `CONTAINER_HOST` was
  already a `unix://` path, so a process that latched onto a dead socket
  kept it for its whole life — days, inside an editor — and dropping the
  cached clients left that path in place, so the retry reconnected to the
  same dead socket. Liveness is now the test, an existing setting is
  re-validated rather than trusted, and the transport is dropped alongside
  the clients.

  Resolution also stopped consulting the podman CLI on the happy path.
  Once liveness is the test, any live socket is usable whichever machine
  put it there, so a subprocess with a multi-second timeout left both
  sandbox creation and the garbage-collection sweep.

- **`timeout` did not stop anything.** The execution backend's timeout is a
  host-side thread join; the container-level cancellation its documentation
  promises is a no-op. `run(code="while True: pass", timeout=30)` returned
  `timed_out: true` while the code kept consuming the sandbox's entire CPU
  ceiling until the sandbox was destroyed, and a sandbox that had been
  reattached to was left unusable afterwards. Submitted code is now killed
  for real, the sandbox stays sealed and usable, and a 5-second timeout
  returns in about 7 seconds instead of never stopping.

- **The language image was deleted after every use.** The backend removes
  an image it pulled when the session closes, and destroying a sandbox
  closes the session before removing the container — so the check for other
  users of the image passed. For the ordinary one-sandbox-at-a-time loop
  that meant a multi-gigabyte re-pull on every create.

- **Startup no longer waits for a garbage-collection sweep.** The first
  sweep ran before the server began listening, so a stopped engine delayed
  every client's first tool call. It now runs on the collector thread.

- **The file lock honours the timeout it advertises.** On macOS and Linux
  it blocked forever instead, so a second client touching the same sandbox
  waited with no deadline and no way to report it, and the client killed
  the request.

### Added

- `hyperbox config --local` pins the configuration to the executable in the
  environment you run it from, instead of resolving `hyperbox` through
  PATH. For testing an unreleased checkout on a machine that also has a
  release installed, where the generated config would otherwise launch the
  release and look entirely correct.

## [0.2.1] — 2026-09-07

First public release. No behaviour changes — everything here is about
being installable and readable by someone who did not write it.

### Added

- Published to PyPI: `pip install hyperbox-mcp`.
- A dedicated PyPI project page, separate from the GitHub README. The
  README opens with a mermaid diagram, which PyPI renders as a raw code
  block, and links to paths in the repo tree that do not resolve on a
  package page.
- Trusted publishing from GitHub Actions (OIDC), so no API token exists
  in the repository, in its secrets, or on a laptop. Releases carry
  PEP 740 attestations.
- CI running the host-side suite on Linux, macOS and Windows across
  Python 3.11 and 3.13.
- `CONTRIBUTING.md` and this changelog.

### Changed

- Documentation reduced to four public pages — README, setup, security
  model, troubleshooting — from ten. The five per-client config pages are
  folded into `docs/setup.md`.
- The YAML client config format is described by its shape, a list-shaped
  `mcpServers` config as used by Continue-based clients, rather than by
  one specific tool's name.
- Licence metadata uses the PEP 639 SPDX expression (`License-Expression:
  MIT`) instead of the deprecated table form.

## [0.2.0] — 2026-09-06

Tagged but never published to an index; its contents ship in 0.2.1.

### Added

- **Custom environments.** `create_sandbox(environment="name")` starts a
  sandbox from a prebuilt image, so a project need not reinstall packages
  on every new sandbox. Build them with `hyperbox build <name> --custom
  <Dockerfile>`; list them with `hyperbox envs`.
- **Server logging.** A rotating log at `~/.hyperbox/logs/server.log`
  (5 MB, three kept), readable with `hyperbox logs [--follow]`. A stdio
  server cannot print, so this is the only record of what happened.
- **Periodic garbage collection.** Expired sandboxes are now reclaimed
  while the server runs, not only at startup.
- **Antigravity client config**: `hyperbox config --format antigravity`.

### Changed

- **State moved to `~/.hyperbox/state`**, beside logs and environments.
  An existing registry at `~/.local/state/hyperbox-mcp/` is migrated on
  first start. The migration never overwrites a registry already at the
  new path, moves only `registry.db` and its WAL siblings, and leaves the
  old directory in place. `XDG_STATE_HOME` is no longer consulted — set
  `HYPERBOX_STATE_DIR` to override.
- `hyperbox logs` is implemented in Python rather than shelling out to
  `tail`, which does not exist on Windows.
- The `Runtime` protocol's `create()` signature gained `environment`.

### Notes

- **The MCP tool surface is unchanged at three tools.** Building an
  environment is deliberately a CLI action: a build runs arbitrary
  commands as root with network access, under none of a sandbox's limits.
- Selecting an environment changes only the base image. Every limit is
  still applied and still verified against the created container.
- Verified against real containers on both engines: 6/6 suites on Docker
  and Podman, 129 checks.

## [0.1.2] — 2026-09-06

### Added

- `hyperbox config` generates ready-to-paste MCP client configuration for
  Claude Desktop, Cursor / VS Code and Continue-based clients, so paths and Windows
  escaping are never typed by hand.

## [0.1.1] — 2026-09-05

### Fixed

- Engine identification and recovery from dropped connections across
  every engine call.
- Three failures found running on Windows.
- Removed the exact-Python pin.

## [0.1.0]

Initial release: the three-tool sandbox lifecycle, server-enforced
resource policy, network sealing, a durable cross-process registry, and
Docker plus Podman support — each proven by acceptance suites running
against real containers.
