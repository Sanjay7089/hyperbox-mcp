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

import json
import os
import time
from pathlib import Path

# --- what we promise we can deliver -------------------------------------
#
# An entry in either map is a promise the tool can deliver that
# environment. Nothing is added until the full suite passes for it
# against a real container. v0.1 ships python only; the javascript,
# ruby and go snippet sets are kept in tests/verify.py so each can be
# re-promoted the moment it clears the hardened suite.

LANGUAGES = ("python",)

# --- environment resolution (call-time, not import-time) ----------------
#
# The only built-in is python — its image is published and pulled by
# `doctor --pull`. Everything else is discovered from
# ~/.hyperbox/environments/*/Dockerfile, written there by `hyperbox
# build`.
#
# This is resolved on every call rather than once at import because
# building happens in a DIFFERENT process: `hyperbox build` is a CLI
# subcommand, while the server is a long-lived stdio process. A map
# computed at import can never see an environment built after the server
# started, and the server can run for days. Caching on the directory's
# mtime keeps the hot path to one stat() instead of a walk.
#
# Limitation, stated honestly: a directory's mtime changes when entries
# are added or removed, not when a Dockerfile inside one is edited. That
# is correct here — we consume the image tag, which does not change when
# its Dockerfile does; rebuilding is `hyperbox build`, and that replaces
# the image in place.

_BUILTIN_ENVIRONMENTS: dict[str, str] = {
    "python": "ghcr.io/vndee/sandbox-python-311-bullseye:latest",
}

def env_dir() -> Path:
    """Where locally built environments live.

    Resolved per call and overridable with HYPERBOX_ENV_DIR, mirroring
    HYPERBOX_STATE_DIR. Two acceptance runs against different runtimes
    otherwise share this directory and each sees environments the other
    built, which reads as a failure in whichever ran second.
    """
    override = os.environ.get("HYPERBOX_ENV_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hyperbox" / "environments"


_env_cache: dict[str, str] | None = None
_env_mtime: float = 0.0
_env_root: Path | None = None

#: How long a directory's mtime stays untrusted after the moment it records.
#:
#: The cache is invalidated by the mtime changing, which assumes the
#: filesystem records a different mtime for two writes a moment apart. On
#: Windows a directory's timestamp is coarse enough that it does not: a
#: `hyperbox build` finishing within one tick of the server's last resolution
#: leaves the mtime identical, so the stale map is served and the new
#: environment stays invisible until something else touches the directory —
#: which is the one promise call-time resolution exists to keep.
#:
#: So an mtime is trusted only once it is old enough that no later write could
#: still share its tick. Inside the window every call re-scans, which is
#: exactly the moment a re-scan is wanted; a server that has been up for hours
#: sees an old mtime and still pays one stat(). Reading st_mtime_ns instead
#: would not help — the value the filesystem records is the problem, not the
#: precision we read it at.
_MTIME_SETTLE_SECONDS = 2.0


def _image_from_manifest(directory: Path) -> str | None:
    """The image an environment's manifest names, if it has one.

    A pulled image has no Dockerfile and no predictable tag, so the
    directory alone stopped being able to say what to run. An unreadable
    manifest returns None rather than raising: one corrupt environment must
    not make every other one unresolvable.
    """
    manifest = directory / "env.json"
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("image") or None
    except (OSError, json.JSONDecodeError):
        return None


def environments() -> dict[str, str]:
    """Resolve the environment map, cached on the environment directory's mtime.

    Built-ins are always present. A custom environment appears as soon as
    its directory holds a Dockerfile — no server restart needed. A mtime
    younger than _MTIME_SETTLE_SECONDS is re-scanned rather than trusted;
    see the constant for why an unchanged mtime is not proof of no change.

    Always returns a fresh dict: callers must never be handed the cache
    itself, or a caller that mutates the result corrupts every later one.
    """
    global _env_cache, _env_mtime, _env_root

    directory = env_dir()
    if directory != _env_root:
        # The location moved (a test isolating itself, an override set
        # after import). A cache keyed only on mtime would happily serve
        # the previous directory's contents.
        _env_cache, _env_mtime, _env_root = None, 0.0, directory

    if not directory.exists():
        return dict(_BUILTIN_ENVIRONMENTS)

    try:
        current_mtime = directory.stat().st_mtime
    except OSError:
        # An unreadable environment directory is not a reason to fail a
        # sandbox that asked for a built-in.
        return dict(_BUILTIN_ENVIRONMENTS)

    settled = time.time() - current_mtime > _MTIME_SETTLE_SECONDS
    if _env_cache is not None and current_mtime == _env_mtime and settled:
        return dict(_env_cache)

    result = dict(_BUILTIN_ENVIRONMENTS)
    for item in sorted(directory.iterdir()):
        if not item.is_dir() or item.name in result:
            # A built-in name is never shadowed by a local directory.
            continue
        image = _image_from_manifest(item)
        if image is None and (item / "Dockerfile").exists():
            # v0.2 directories have no manifest. The tag it would have
            # produced is still the right answer, so they keep working.
            image = f"hyperbox-local/{item.name}:latest"
        if image:
            result[item.name] = image
    _env_cache = result
    _env_mtime = current_mtime
    return dict(result)


BACKENDS = ("docker", "podman")

#: Backends that are wired up but not proven by the full suite. Empty:
#: both engines pass every suite against real containers. Podman needs
#: its transport chosen deliberately (see engine.ensure_podman_transport)
#: — over a TCP forward it returns correct exit codes and no output at
#: all. `auto` still prefers docker, purely as a stable default.
EXPERIMENTAL_BACKENDS: tuple[str, ...] = ()

#: Accepted by `create_sandbox`; "auto" resolves to the first reachable
#: engine. Never a passthrough for arbitrary engine options.
BACKEND_CHOICES = ("auto", *BACKENDS)

# --- resource ceilings ---------------------------------------------------

MEM_LIMIT = "1g"
MEM_LIMIT_BYTES = 1024 * 1024 * 1024
CPUS = 1.0
PIDS_LIMIT = 128

#: The memory ceiling INCLUDING swap, which is what actually bounds a
#: container. Equal to MEM_LIMIT_BYTES, so swap is disabled and the limit
#: means what `hyperbox://capabilities` says it means.
#:
#: Setting Memory without MemorySwap does not do what it looks like it
#: does. Docker then defaults MemorySwap to TWICE Memory, so a sandbox
#: advertising "1g" ran with a 2 GB ceiling. Measured on Docker 29.1.3:
#:
#:     --memory=1g                  -> MemorySwap 2147483648, and
#:                                     bytearray(2 * 1024**3) SUCCEEDS
#:     --memory=1g --memory-swap=1g -> MemorySwap 1073741824, and the
#:                                     same allocation is killed (137)
#:
#: Podman does not double it and enforced 1 GB either way, so this was
#: invisible on one engine and wrong on the other -- the same shape as
#: the CPU story below. Both engines record MemorySwap in inspect, so it
#: is read back with everything else.
#:
#: The two engines also FAIL differently, which any test here must allow
#: for: Docker OOM-kills the process (137) while Podman refuses the
#: allocation (MemoryError). Assert that the allocation did not succeed,
#: never a specific exit code.
MEM_SWAP_BYTES = MEM_LIMIT_BYTES

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

#: Where submitted code is written. Deliberately NOT under TMPFS_PATHS.
#:
#: The engine's archive API cannot write through a tmpfs mount on Docker:
#: it writes into the image layer underneath, where the mount shadows it,
#: and returns 200 having done nothing observable. Podman writes through,
#: so this is invisible on one engine and fatal on the other -- a silent
#: no-op of exactly the kind the read-back checks exist to catch.
#:
#: /work stays the caller's scratch space. Code running inside the sandbox
#: writes there normally; only the archive API cannot.
CODE_DIR = "/sandbox"

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

#: How often the background sweep runs while the server is up. The
#: inactivity TTL is otherwise enforced only by restarting the process,
#: and a server inside an editor can stay up for days.
GC_INTERVAL_SECONDS = 300.0

#: A container younger than this is never reclaimed by GC, even with no
#: registry row. Belt-and-braces behind the registry's create-time
#: reservation: another process may be mid-create right now.
GC_GRACE_SECONDS = 120.0

#: Inactivity TTL. A sandbox untouched for this long becomes reclaimable.
DEFAULT_TTL_SECONDS = float(os.environ.get("HYPERBOX_TTL_SECONDS", 30 * 60))
