"""Server policy: the limits and the supported surface.

Every value here is set by the server and cannot be raised by a caller.
No tool takes a parameter that overrides any of it. That is the whole
point — the agent proposes what to run, the server decides the ceiling it
runs under.

This module is deliberately free of backend imports. Both the MCP layer
and the concrete Runtime read their policy from here, so neither has to
import the other to learn what a sandbox is allowed to do.
"""

from __future__ import annotations

import os

# --- what we promise we can deliver -------------------------------------
#
# An entry in either map is a promise the tool can deliver that
# environment. Nothing is added until the full suite passes for it
# against a real container. v0.1 ships python only; the javascript,
# ruby and go snippet sets are kept in tests/verify.py so each can be
# re-promoted the moment it clears the hardened suite.

LANGUAGES = ("python",)

BACKENDS = ("docker", "podman")

#: Reachable, wired up, and NOT proven. Podman is experimental: on the
#: versions tested, podman-py's exec_run returns no output at all in any
#: mode, so results never reach the server. Sandboxes refuse to start on
#: it rather than answering every run with empty output — see
#: docs/troubleshooting.md. `auto` prefers docker for this reason.
EXPERIMENTAL_BACKENDS = ("podman",)

#: Accepted by `create_sandbox`; "auto" resolves to the first reachable
#: engine. Never a passthrough for arbitrary engine options.
BACKEND_CHOICES = ("auto", *BACKENDS)

# --- resource ceilings ---------------------------------------------------

MEM_LIMIT = "1g"
MEM_LIMIT_BYTES = 1024 * 1024 * 1024
CPUS = 1.0
PIDS_LIMIT = 128

#: The same ceiling, spelled two ways, because the clients disagree.
#: docker-py takes `nano_cpus`; podman-py silently DISCARDS that keyword
#: (it is in its "Ignore these keywords" list) and honours only
#: `cpu_period` / `cpu_quota`. Passing nano_cpus to podman produced a
#: container with CpuQuota=0 — no CPU limit at all — while the server
#: went on describing it as limited. Both spellings are checked against
#: the created container.
NANO_CPUS = int(CPUS * 1_000_000_000)
CPU_PERIOD = 100_000
CPU_QUOTA = int(CPU_PERIOD * CPUS)

#: Bounded scratch space, declared as paths and a size rather than as one
#: engine's syntax: Docker takes a `tmpfs` mapping while Podman rejects
#: that keyword and wants tmpfs entries in `mounts`. The runtime builds
#: whichever shape the chosen engine accepts.
#:
#: These are tmpfs, so they are accounted against the container's memory
#: cgroup — a full /work eats into MEM_LIMIT rather than growing the
#: host's disk without limit.
#: Only /work. A sized tmpfs over /tmp was tried and is not portable:
#: podman's crun refuses it with "No space left on device" whatever
#: options are given, while an unsized one defaults to half of RAM and
#: so is not a limit at all. /tmp remains writable and is still
#: destroyed with the container, but /work is the bounded one and is
#: what the tools point callers at.
TMPFS_SIZE = "64m"
TMPFS_PATHS = ("/work",)

#: Blocks setuid/setgid escalation inside the container. Safe on every
#: image; unlike cap_drop it does not interfere with the workdir chown
#: the backend performs during environment setup.
NO_NEW_PRIVILEGES = True

# --- execution ceilings --------------------------------------------------

MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_CHARS = 20_000
MAX_CODE_CHARS = 1024 * 1024
MAX_LIBRARIES = 25

# --- ownership -----------------------------------------------------------

#: Every container we create carries both labels. GC matches on them and
#: ONLY on them — a container without our label is never ours to remove.
LABEL_MANAGED = "hyperbox-mcp.managed"
LABEL_ID = "hyperbox-mcp.id"

#: A container younger than this is never reclaimed by GC, even with no
#: registry row. Belt-and-braces behind the registry's create-time
#: reservation: another process may be mid-create right now.
GC_GRACE_SECONDS = 120.0

#: Inactivity TTL. A sandbox untouched for this long becomes reclaimable.
DEFAULT_TTL_SECONDS = float(os.environ.get("HYPERBOX_TTL_SECONDS", 30 * 60))
