"""Terminal commands for looking after sandboxes: init, ps, rm, pull.

These exist because everything else about a sandbox's life happens
through an MCP client, and there was no way to see what was running or
clean up after a client that went away without one.

`pull` is the only place in HyperBox where bytes the sandbox produced
land on the host filesystem, so its extraction treats the archive as
hostile. See extract_safely.
"""

from __future__ import annotations

import sys
import tarfile
import time
from pathlib import Path

from hyperbox_mcp import errors, policy, validate
from hyperbox_mcp.registry import Registry

#: A pulled archive is written by code that ran in the sandbox. These
#: bound what a hostile one can do to the host before anything is
#: written, so a tar bomb is refused rather than absorbed.
PULL_MAX_BYTES = 256 * 1024 * 1024
PULL_MAX_FILES = 10_000


def init_sync_root(directory: str | None = None) -> int:
    """Allow syncing from a directory. A human action, deliberately.

    The whole value of the sync gate is that a PERSON chose which
    directories a sandbox may read. Agents have shell access in the
    clients HyperBox targets, so an agent able to run this could widen
    its own boundary — which would leave the gate as decoration. Hence
    the TTY requirement: it cannot be done by something that is not
    sitting at a terminal.
    """
    target = Path(directory or Path.cwd()).expanduser().resolve()
    if not target.is_dir():
        print(f"hyperbox init: {target} is not a directory", file=sys.stderr)
        return 2

    if not sys.stdin.isatty():
        print(
            "hyperbox init must be run by a person at a terminal.\n"
            "\n"
            "It decides which directories a sandbox may read from this\n"
            "machine. Running it from a script or an agent would let the\n"
            "thing being sandboxed choose what it is allowed to see.\n"
            f"\nTo allow {target}, run this yourself:\n"
            f"    hyperbox init {target}",
            file=sys.stderr,
        )
        return 2

    print(f"Allow HyperBox sandboxes to read files from:\n    {target}\n")
    print("Anything under it can be copied into a sandbox by an agent that")
    print("asks for it. Secrets (.env, .aws/, id_rsa) are never copied.\n")
    answer = input("Allow this directory? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Nothing changed.")
        return 1

    path = policy.sync_roots_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.is_file():
        existing = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if str(target) in existing:
        print(f"Already allowed. {path} is unchanged.")
        return 0
    existing.append(str(target))
    path.write_text(
        "# Directories HyperBox sandboxes may be given files from.\n"
        "# One absolute path per line. Written by `hyperbox init`.\n"
        + "\n".join(existing) + "\n",
        encoding="utf-8",
    )
    print(f"Allowed. Recorded in {path}")
    print("A running server picks this up without a restart.")
    return 0


def _age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def list_sandboxes() -> int:
    """Every sandbox on file, whoever created it.

    Read from the registry rather than the engine, because the registry
    is the record of what HyperBox believes it owns — and a difference
    between the two is exactly what someone running this wants to see.
    """
    records = sorted(Registry().all_records(), key=lambda r: r.created_at)
    if not records:
        print("No sandboxes on file.")
        return 0

    now = time.time()
    print(f"{'SANDBOX':<14} {'LANG':<11} {'BACKEND':<9} {'STATE':<9} "
          f"{'IDLE':<7} {'EXPIRES IN':<11} CONTAINER")
    print("-" * 88)
    for r in records:
        remaining = r.expires_at - now
        expires = "expired" if remaining <= 0 else _age(remaining)
        print(
            f"{r.sandbox_id:<14} {r.language:<11} {r.backend:<9} "
            f"{r.state:<9} {_age(now - r.last_used_at):<7} {expires:<11} "
            f"{(r.container_ref or '')[:12]}"
        )
    print(f"\n{len(records)} sandbox(es). `hyperbox rm <id>` removes one.")
    return 0


def remove_sandbox(sandbox_id: str) -> int:
    """Destroy one sandbox from the terminal.

    Goes through the same runtime and the same registry rules as the MCP
    tool rather than reaching for the engine directly: destroy, confirm
    it is actually gone, and only then drop the row. A container that
    could not be reached has NOT been proven gone, and its row stays so
    garbage collection tries again.
    """
    try:
        sandbox_id = validate.sandbox_id(sandbox_id)
    except errors.InvalidInput as exc:
        print(f"hyperbox rm: {exc}", file=sys.stderr)
        return 2

    # Imported here, not at module scope: `ps` and `init` must not pay
    # for the MCP server just to read a database or write a text file.
    from hyperbox_mcp.server import _handle_for, select_runtime

    registry = Registry()
    runtime = select_runtime()
    with registry.lock(sandbox_id):
        record = registry.get(sandbox_id)
        if record is None:
            print(f"{sandbox_id}: already gone")
            return 0
        handle = _handle_for(record)
        try:
            runtime.destroy(handle)
        except errors.HyperBoxError as exc:
            print(f"hyperbox rm: {exc}", file=sys.stderr)
            print(f"{sandbox_id} is still on file; it has not been proven "
                  "gone.", file=sys.stderr)
            return 1
        if runtime.alive(handle):
            print(f"hyperbox rm: {sandbox_id} is still running after "
                  "destroy; left on file.", file=sys.stderr)
            return 1
        registry.remove(sandbox_id)
    print(f"{sandbox_id}: destroyed")
    return 0


def extract_safely(blob: bytes, dest: Path) -> tuple[int, list[str]]:
    """Extract a tar the SANDBOX produced, onto the host.

    Everything in this archive was written by code running in a sandbox,
    which is to say by whatever the agent decided to run. A member named
    `../../.ssh/authorized_keys` extracts outside `dest` unless something
    stops it, and tarfile.extractall did not stop it by default for
    fifteen years (CVE-2007-4559).

    Python's fix, `filter="data"`, arrived in 3.12 and was backported to
    3.11.4 — and this project supports 3.11, so it cannot be assumed. It
    is used when present and this FAILS CLOSED when it is not, rather
    than falling back to an unfiltered extract.

    The filter is the security boundary, and it is enough on its own:
    measured, `extractall(..., filter="data")` refuses
    `../../../../tmp/x` with OutsideDestinationError. The member checks
    below are not a second boundary and are not claimed as one. They
    exist because the filter aborts the WHOLE extraction on the first bad
    member, so one odd file a sandbox happened to write loses the entire
    pull. Screening first means the good files land and every refusal is
    reported with a reason.
    """
    import io

    refused: list[str] = []
    root = dest.resolve()
    root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > PULL_MAX_FILES:
            raise errors.InvalidInput(
                f"The archive holds {len(members)} entries, over the "
                f"{PULL_MAX_FILES} limit.",
                fix="Pull a narrower path.",
            )
        total = sum(m.size for m in members)
        if total > PULL_MAX_BYTES:
            raise errors.InvalidInput(
                f"The archive is {total / 1024 / 1024:.0f} MB, over the "
                f"{PULL_MAX_BYTES // 1024 // 1024} MB limit.",
                fix="Pull a narrower path.",
            )

        safe = []
        for member in members:
            name = member.name
            if member.issym() or member.islnk() or member.isdev():
                refused.append(f"{name} ({'link' if not member.isdev() else 'device'})")
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                refused.append(f"{name} (path escapes the destination)")
                continue
            resolved = (root / name).resolve()
            if resolved != root and root not in resolved.parents:
                refused.append(f"{name} (resolves outside the destination)")
                continue
            safe.append(member)

        try:
            archive.extractall(root, members=safe, filter="data")
        except TypeError:
            # No data filter on this interpreter. The checks above already
            # removed traversal, links and devices, but the filter also
            # normalises modes and ownership, and shipping a weaker
            # extraction silently is exactly the habit this codebase
            # exists to avoid.
            raise errors.InvalidInput(
                f"This Python ({sys.version.split()[0]}) has no tar data "
                "filter, so an archive written inside a sandbox cannot be "
                "extracted safely.",
                fix="Upgrade to Python 3.11.4 or newer.",
            ) from None

    return len(safe), refused


def pull_from_sandbox(sandbox_id: str, path: str, dest: str) -> int:
    """Copy a path out of a sandbox onto the host."""
    try:
        sandbox_id = validate.sandbox_id(sandbox_id)
    except errors.InvalidInput as exc:
        print(f"hyperbox pull: {exc}", file=sys.stderr)
        return 2

    from hyperbox_mcp import engine
    from hyperbox_mcp.rest import api
    from hyperbox_mcp.rest.client import EngineClient

    registry = Registry()
    record = registry.get(sandbox_id)
    if record is None:
        print(f"hyperbox pull: no sandbox '{sandbox_id}' on file. "
              "`hyperbox ps` lists them.", file=sys.stderr)
        return 1

    resolution = engine.resolve(record.backend)
    client = EngineClient(resolution.endpoint)
    try:
        blob = api.get_archive(client, record.container_ref, path)
    except errors.HyperBoxError as exc:
        print(f"hyperbox pull: {exc}", file=sys.stderr)
        return 1

    target = Path(dest).expanduser()
    try:
        count, refused = extract_safely(blob, target)
    except errors.HyperBoxError as exc:
        print(f"hyperbox pull: {exc}", file=sys.stderr)
        return 1

    print(f"Pulled {count} file(s) from {sandbox_id}:{path} to {target}")
    for entry in refused:
        print(f"  refused: {entry}", file=sys.stderr)
    return 0
