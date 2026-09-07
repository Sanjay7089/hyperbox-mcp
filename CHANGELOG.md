# Changelog

All notable changes to HyperBox are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
