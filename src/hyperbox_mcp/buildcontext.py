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
