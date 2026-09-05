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

#: Accepted by `create_sandbox`; "auto" resolves to the first reachable
#: engine. Never a passthrough for arbitrary engine options.
BACKEND_CHOICES = ("auto", *BACKENDS)

# --- resource ceilings ---------------------------------------------------

MEM_LIMIT = "1g"
MEM_LIMIT_BYTES = 1024 * 1024 * 1024
NANO_CPUS = 1_000_000_000  # 1 CPU
PIDS_LIMIT = 128

#: Bounded scratch space. These are tmpfs, so they are accounted against
#: the container's memory cgroup — a full /work eats into MEM_LIMIT
#: rather than growing the host's disk without limit.
TMPFS_SIZE = "64m"
TMPFS = {
    "/work": f"rw,size={TMPFS_SIZE},mode=1777",
    "/tmp": f"rw,size={TMPFS_SIZE},mode=1777",
}

#: Blocks setuid/setgid escalation inside the container. Safe on every
#: image; unlike cap_drop it does not interfere with the workdir chown
#: llm-sandbox performs during environment setup.
SECURITY_OPT = ["no-new-privileges"]

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
