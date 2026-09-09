"""Assembling a build context the way the engine expects it.

The build API accepts a tar and nothing else, and it does NOT read
`.dockerignore` — the client is expected to have excluded already. Get that
wrong and a user sends `node_modules/` or a `.git` history over a socket
and waits, with no indication of why a two-file build took ten minutes.

Kept apart from the builder so the exclusion rules can be tested without
an engine.
"""

from __future__ import annotations

import io
import tarfile
from fnmatch import fnmatch
from pathlib import Path

#: Always excluded, whatever .dockerignore says. These cannot be needed by
#: a build and are the ones that make a context enormous by surprise.
ALWAYS_EXCLUDE = (".git", "__pycache__", ".venv", "venv", "node_modules")


def read_dockerignore(context: Path) -> list[str]:
    """Patterns from .dockerignore, comments and blanks dropped."""
    path = context / ".dockerignore"
    if not path.is_file():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(line.rstrip("/"))
    return patterns


def excluded(relative: str, patterns: list[str]) -> bool:
    """Whether a path is excluded, by our own rules or the user's.

    Matched per path segment as well as whole, because `node_modules` in a
    .dockerignore is expected to exclude everything beneath it, not only a
    file with that exact name.
    """
    parts = Path(relative).parts
    if any(part in ALWAYS_EXCLUDE for part in parts):
        return True
    for pattern in patterns:
        if fnmatch(relative, pattern) or fnmatch(parts[0], pattern):
            return True
        if any(fnmatch(part, pattern) for part in parts):
            return True
    return False


def build_tar(context: Path, dockerfile: Path | None = None) -> tuple[bytes, str, int]:
    """Tar a build context. Returns (bytes, dockerfile name, files included).

    A Dockerfile outside the context directory is copied in under a
    generated name, so `--dockerfile ../shared/Dockerfile` works without
    the caller having to reorganise their tree.
    """
    patterns = read_dockerignore(context)
    buffer = io.BytesIO()
    included = 0
    name = "Dockerfile"

    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(context.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(context))
            if excluded(relative, patterns):
                continue
            archive.add(path, arcname=relative)
            included += 1

        if dockerfile is not None:
            resolved = dockerfile.resolve()
            inside = resolved.is_relative_to(context.resolve())
            if inside:
                name = str(resolved.relative_to(context.resolve()))
                if excluded(name, patterns):
                    # A .dockerignore that excludes the Dockerfile itself
                    # would otherwise produce "Cannot locate Dockerfile"
                    # from the engine, which names the wrong cause.
                    archive.add(resolved, arcname=name)
                    included += 1
            else:
                name = ".hyperbox.Dockerfile"
                data = resolved.read_bytes()
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
                included += 1

    return buffer.getvalue(), name, included


def read_ignore(source: Path, filename: str) -> list[str]:
    """Patterns from an ignore file, comments and blanks dropped."""
    path = source / filename
    if not path.is_file():
        return []
    patterns = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(line.rstrip("/"))
    return patterns


def sync_tar(
    source: Path,
    *,
    ignore_file: str,
    deny_files: tuple[str, ...],
    deny_dirs: tuple[str, ...],
    max_bytes: int,
    max_files: int,
) -> tuple[bytes, dict]:
    """Tar a host directory to sync into a sandbox.

    Returns (tar bytes, manifest). The manifest records what went in AND
    everything that did not, with a reason for each — because a file that
    silently fails to arrive is indistinguishable, from inside the
    sandbox, from a file the agent never wrote. It then invents an
    explanation. Naming the exclusion is the whole point.

    Deliberately reads `ignore_file` (.hyperboxignore) rather than
    .dockerignore. A .dockerignore describes what should not go into a
    production IMAGE, and a project that excludes tests/ or *.sql from
    its image excludes exactly what an integration run needs synced.

    Over a cap this RAISES rather than truncating. A half-copied
    directory is the silent-wrong-answer failure again: the sandbox
    looks populated and is not.
    """
    patterns = read_ignore(source, ignore_file)
    buffer = io.BytesIO()
    skipped: list[dict] = []
    included = 0
    total = 0

    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(source.rglob("*")):
            relative = str(path.relative_to(source))
            parts = Path(relative).parts

            if path.is_symlink():
                # Not followed: a link inside the tree can point anywhere,
                # including outside every allowed root.
                skipped.append({"path": relative, "reason": "symlink"})
                continue
            if not path.is_file():
                continue
            if path.name in deny_files or any(p in deny_dirs for p in parts):
                skipped.append({"path": relative, "reason": "secret"})
                continue
            if excluded(relative, patterns):
                skipped.append({"path": relative, "reason": "ignored"})
                continue

            size = path.stat().st_size
            if included + 1 > max_files:
                raise ValueError(
                    f"{source} holds more than {max_files} files to sync. "
                    "Narrow sync_in_dir to the directory actually needed, "
                    f"or exclude what is not in a {ignore_file}."
                )
            if total + size > max_bytes:
                limit = (
                    f"{max_bytes / (1024 * 1024):.0f} MB"
                    if max_bytes >= 1024 * 1024
                    else f"{max_bytes} bytes"
                )
                raise ValueError(
                    f"{source} is larger than the {limit} sync limit "
                    f"(reached at {relative}). Narrow sync_in_dir, or "
                    f"exclude what is not needed in a {ignore_file}."
                )
            archive.add(path, arcname=relative)
            included += 1
            total += size

    return buffer.getvalue(), {
        "files": included,
        "bytes": total,
        "skipped": skipped,
    }
