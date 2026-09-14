"""Strict input validation for everything a caller can send.

Pure functions, no I/O. Each raises `InvalidInput` with a message that
says what to pass instead, because the caller is usually a language model
that will read the error and retry — a message that only says "invalid"
costs a whole round trip to learn nothing. One exception: `language()`
raises `UnsupportedLanguageError` instead, because "you asked for
something that doesn't exist" is a menu problem an agent can act on
differently from a malformed argument -- see native_runtime._spec, which
raises the same class for the same reason if this check is ever bypassed.

Rules enforced here, and why each exists:

- A timeout must be a finite positive number. `min(float('nan'), 60)`
  returns nan, which then flows into the backend as a timeout that never
  fires, so NaN has to be rejected explicitly rather than clamped.
- A sandbox id must match the exact shape we mint. The registry derives a
  lock filename from the id by stripping non-alphanumerics, so `a/b` and
  `ab` would otherwise contend on one lock file.
- A library name must be a package name, not a flag. The install command
  is built as `pip install {library} ...` and executed via shlex-split
  argv — so shell metacharacters are inert, but `--index-url http://...`
  is a live pip flag, and it would take effect during the one window
  where the sandbox has network access.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from hyperbox_mcp import errors, policy
from hyperbox_mcp.policy import (
    BACKEND_CHOICES,
    LANGUAGES,
    MAX_CODE_CHARS,
    MAX_LIBRARIES,
    MAX_TIMEOUT_SECONDS,
)


#: Defined in errors.py; still a ValueError, now with a code.
InvalidInput = errors.InvalidInput


_SANDBOX_ID = re.compile(r"^[0-9a-f]{12}$")

# A conservative subset of PEP 508: name, optional extras, optional
# version specifier. Deliberately narrower than pip accepts — no URLs,
# no local paths, no VCS references, no environment markers. Those are
# all legitimate pip features and all of them would let a caller fetch
# and execute arbitrary code during the network window.
_LIBRARY_PYTHON = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?"       # package name
    r"(\[[A-Za-z0-9._,-]+\])?"                          # optional extras
    r"((==|>=|<=|~=|!=|<|>)[A-Za-z0-9._*+!-]+)?$"       # optional version
)

# A bare name segment, shared by the javascript and go patterns below:
# alphanumerics plus the punctuation real registries actually use inside
# one path component, starting and ending on an alphanumeric so a lone
# `.`/`-`/`_` (or a leading `-`, which would otherwise read as a flag)
# never matches.
_NAME_SEGMENT = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"

# npm syntax: `name`, `name@1.2.3`, `name@^1.2.3`, and scoped packages
# `@scope/name` / `@scope/name@1.2.3`. `==` (pip's operator) is not valid
# npm syntax at all, which is exactly the bug this table fixes — npm
# separates name from version with `@` and spells ranges with a leading
# `^`/`~`, not a comparison operator. No character here is one a URL,
# `git+...` reference, or local path needs (no `:`, no `/` outside the
# one scope separator, no leading `-` or `.`).
_LIBRARY_JAVASCRIPT = re.compile(
    rf"^(@{_NAME_SEGMENT}/)?{_NAME_SEGMENT}"
    rf"(@[\^~]?{_NAME_SEGMENT})?$"
)

# `go get` syntax: a module path (domain-style segments joined by `/`,
# e.g. `github.com/spf13/cobra`) with an optional `@version`, where a
# real version conventionally starts with `v` (`@v1.8.0`) but `@latest`
# and similar pseudo-versions are also legal. No scheme (`https://`) is
# accepted — go module paths never carry one, and allowing `:` would
# reopen exactly the URL hole this validator exists to close.
_LIBRARY_GO = re.compile(
    rf"^{_NAME_SEGMENT}(?:/{_NAME_SEGMENT})*"
    rf"(@[A-Za-z0-9][A-Za-z0-9._+-]*)?$"
)

# apt syntax: `name` or `name=1.2.3-1`. apt uses `=`, never `==`, and a
# Debian version string legitimately contains `:` (an epoch, e.g.
# `2:8.32-4.1`) and `~` (pre-release ordering) — both excluded from the
# name half so `pkg=1:2.0` can't be misread as two packages.
_APT_NAME = r"[a-z0-9][a-z0-9.+-]*"
_APT_VERSION = r"[A-Za-z0-9][A-Za-z0-9.:+~-]*"
_LIBRARY_BASH = re.compile(rf"^{_APT_NAME}(={_APT_VERSION})?$")

#: Per-language version-specifier syntax. Java is absent on purpose: its
#: LANGUAGES entry has `install: None`, so any package name is refused
#: downstream regardless of shape — there is no version syntax to accept.
#: Callers for java (and anything else not listed) fall back to the
#: python pattern below, matching this module's behaviour before it knew
#: about languages at all.
_LIBRARY_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": _LIBRARY_PYTHON,
    "javascript": _LIBRARY_JAVASCRIPT,
    "go": _LIBRARY_GO,
    "bash": _LIBRARY_BASH,
}

#: A real, correctly-syntaxed example per language, for the error message.
#: Showing a pip example to a caller validating an npm package name is
#: exactly the confusion that made pinning look broken in the first place.
_LIBRARY_EXAMPLES: dict[str, str] = {
    "python": "'requests' or 'pandas==2.2.0'",
    "javascript": "'lodash' or 'mime-db@1.54.0'",
    "go": "'github.com/spf13/cobra' or 'github.com/spf13/cobra@v1.8.0'",
    "bash": "'curl' or 'curl=7.88.1-10'",
}


def sandbox_id(value: object) -> str:
    """A sandbox id in exactly the form `create_sandbox` mints."""
    if not isinstance(value, str):
        raise InvalidInput(
            f"sandbox_id must be a string, got {type(value).__name__}. "
            "Pass the sandbox_id returned by create_sandbox."
        )
    candidate = value.strip()
    if not _SANDBOX_ID.match(candidate):
        raise InvalidInput(
            f"'{value[:64]}' is not a valid sandbox_id. Expected 12 "
            "lowercase hex characters, exactly as returned by "
            "create_sandbox."
        )
    return candidate


def process_id(value: object) -> str:
    """A background run's id, in the form run(background=True) returns.

    Its own shape, not sandbox_id's: a sandbox id is 12 hex characters
    and this is a full uuid4 hex. Sharing the validator would reject
    every real process id.

    It becomes a filename inside the sandbox, so the shape is enforced
    rather than trusted -- an id carrying a slash or a `..` would name a
    path instead of a log.
    """
    if not isinstance(value, str):
        raise InvalidInput(
            f"process_id must be a string, got {type(value).__name__}. "
            "Pass the process_id returned by run(background=True)."
        )
    candidate = value.strip()
    if not re.fullmatch(r"[0-9a-f]{32}", candidate):
        raise InvalidInput(
            f"'{str(value)[:64]}' is not a valid process_id. Expected 32 "
            "lowercase hex characters, exactly as returned by "
            "run(background=True)."
        )
    return candidate


def language(value: object, supported: tuple[str, ...] | None = None) -> str:
    """Validate a language against what the ACTIVE runtime can deliver.

    The supported set is passed in rather than read from policy, because
    the two runtimes differ and an entry in that set is a promise to the
    caller. Defaults to policy.LANGUAGES so existing callers are unchanged.
    """
    allowed = supported if supported is not None else LANGUAGES
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        # UNSUPPORTED_LANGUAGE, not the generic INVALID_INPUT this module
        # otherwise raises everywhere: "you asked for something we've
        # never heard of" is a menu problem an agent can act on
        # differently from a malformed argument, and native_runtime._spec
        # already raises the same class for the same condition if this
        # check is ever bypassed. Two call sites, one code, matched fix
        # shape.
        raise errors.UnsupportedLanguageError(
            f"Unsupported language '{value}'.",
            fix=f"Supported: {', '.join(sorted(allowed))}.",
        )
    return value.strip().lower()


def backend(value: object) -> str:
    if not isinstance(value, str) or value.strip().lower() not in BACKEND_CHOICES:
        raise InvalidInput(
            f"Unsupported backend '{value}'. Supported: "
            f"{', '.join(BACKEND_CHOICES)}. Use 'auto' to let the server "
            "pick whichever engine is running."
        )
    return value.strip().lower()


def timeout(value: object) -> float:
    """A finite positive timeout, clamped to the server ceiling.

    Clamping is the only adjustment made silently, and only downward —
    the ceiling is server policy, so a caller asking for more gets less
    rather than an error. Everything else is rejected outright.
    """
    if value is None:
        raise InvalidInput(
            "timeout=None is not permitted; execution time is server policy. "
            f"Pass a number greater than 0 and up to {MAX_TIMEOUT_SECONDS:g} "
            "seconds."
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInput(
            f"timeout must be a number of seconds, got "
            f"{type(value).__name__}. Pass a number greater than 0 and up "
            f"to {MAX_TIMEOUT_SECONDS:g}."
        )
    seconds = float(value)
    if math.isnan(seconds) or math.isinf(seconds):
        raise InvalidInput(
            f"timeout must be a finite number, got {value}. Pass a number "
            f"greater than 0 and up to {MAX_TIMEOUT_SECONDS:g} seconds."
        )
    if seconds <= 0:
        raise InvalidInput(
            f"timeout must be greater than 0, got {seconds:g}. A "
            "non-positive timeout would leave the sandbox no time to run."
        )
    return min(seconds, MAX_TIMEOUT_SECONDS)


def code(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidInput(
            f"code must be a string, got {type(value).__name__}."
        )
    if not value.strip():
        raise InvalidInput("code is empty; there is nothing to run.")
    if len(value) > MAX_CODE_CHARS:
        raise InvalidInput(
            f"code is {len(value)} characters, over the server limit of "
            f"{MAX_CODE_CHARS}. Write large inputs to a file inside the "
            "sandbox across several runs instead."
        )
    return value


def libraries(value: object, language: str = "python") -> list[str]:
    """Package names only — never flags, URLs, paths or VCS references.

    `language` picks which version-specifier syntax is accepted (pip's
    `==`, npm's `@`, `go get`'s `@`, apt's `=`) — see `_LIBRARY_PATTERNS`.
    It defaults to python so any caller that does not pass one keeps this
    module's original, pip-flavoured behaviour exactly. What counts as a
    bare name versus a flag/URL/path/VCS-reference is NOT language
    dependent and is never broadened here, regardless of `language`.
    """
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise InvalidInput(
            "libraries must be a list of package names, e.g. "
            "['requests', 'pandas==2.2.0']."
        )
    names = list(value)
    if len(names) > MAX_LIBRARIES:
        raise InvalidInput(
            f"{len(names)} libraries requested, over the server limit of "
            f"{MAX_LIBRARIES}. Install the ones you actually import."
        )
    pattern = _LIBRARY_PATTERNS.get(language, _LIBRARY_PYTHON)
    example = _LIBRARY_EXAMPLES.get(language, _LIBRARY_EXAMPLES["python"])
    cleaned: list[str] = []
    for item in names:
        if not isinstance(item, str):
            raise InvalidInput(
                f"Every library must be a string, got "
                f"{type(item).__name__}."
            )
        name = item.strip()
        if not pattern.match(name):
            raise InvalidInput(
                f"'{item[:64]}' is not an accepted package name. Pass a "
                f"plain name with an optional version, like {example}. "
                "Installer flags, URLs, file paths and VCS references are "
                "refused: the install step is the only moment the sandbox "
                "has network access, and it is limited to named packages "
                "from the default index."
            )
        cleaned.append(name)
    return cleaned


# Docker tag component rules: lowercase alphanumerics, dots, hyphens and
# underscores, starting with a letter or digit, 64 chars max. The pattern
# is anchored, so it also blocks path traversal — the name becomes both a
# path segment under ~/.hyperbox/environments/ and an image tag, and
# "../.." must never reach either.
_ENVIRONMENT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def environment_name(value: object) -> str:
    """Validate the SHAPE of an environment name, not its existence.

    `hyperbox build` needs this: the environment it is about to create
    does not exist yet, but its name still becomes a directory under
    ~/.hyperbox and an image tag, so it is checked before either is
    touched.
    """
    if not isinstance(value, str):
        raise InvalidInput(
            f"environment must be a string, got {type(value).__name__}."
        )
    name = value.strip().lower()
    if not _ENVIRONMENT_NAME.match(name):
        raise InvalidInput(
            f"'{value[:64]}' is not a valid environment name. Use lowercase "
            "letters, digits, dots, hyphens or underscores, starting with a "
            "letter or digit (1-64 characters)."
        )
    return name


def environment(value: object) -> str | None:
    """Validate an environment name and that it exists. None = language default.

    Resolved against policy.environments() on every call rather than a
    list captured at import, so an environment built while the server was
    running is accepted without a restart.
    """
    if value is None:
        return None
    name = environment_name(value)
    available = policy.environments()
    if name not in available:
        raise InvalidInput(
            f"Unknown environment '{name}'. "
            f"Available: {', '.join(sorted(available))}. "
            "The user can build one at their terminal with: "
            "hyperbox build <name> --image <ref>   (or --dockerfile <path>)"
        )
    return name


def sync_from(value: object) -> Path | None:
    """Resolve a host directory the caller wants synced in. None = nothing.

    Not a pure function, unlike everything above it: deciding whether a
    path is inside an allowed root means asking the filesystem what the
    path really is. A prefix comparison on the string is not enough --
    a directory inside a root can be a symlink pointing anywhere, so both
    sides are resolved before they are compared.

    The roots come from a file the user writes (see policy.sync_roots).
    Unset means the feature is off, and that is the default: host files
    reach a sandbox only because a human named the directory they may
    come from.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(
            "sync_from must be a path to a directory on the user's "
            "machine, or omitted."
        )

    roots = policy.sync_roots()
    if not roots:
        raise InvalidInput(
            "Syncing host files is not enabled on this machine, so "
            "sync_from cannot be used.",
            fix="The user enables it at their terminal, once, by running "
                "`hyperbox init` in a directory they are willing to share. "
                "You cannot run it for them.",
        )

    try:
        candidate = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise InvalidInput(
            f"sync_from '{value}' does not exist on the user's machine.",
            fix="Ask the user for the correct path, or omit sync_from.",
            context={"path": str(value), "error": str(exc)},
        ) from exc

    if not candidate.is_dir():
        raise InvalidInput(
            f"sync_from '{value}' is not a directory.",
            fix="Pass a directory; a single file cannot be synced.",
        )

    for root in roots:
        if candidate == root or root in candidate.parents:
            return candidate

    raise InvalidInput(
        f"'{candidate}' is outside every directory the user allows "
        "syncing from.",
        fix="Allowed: " + ", ".join(str(r) for r in roots) + ". Ask the "
            "user to run `hyperbox init` in the directory they want to "
            "share, if it should be one of them.",
        context={"requested": str(candidate),
                 "roots": [str(r) for r in roots]},
    )
