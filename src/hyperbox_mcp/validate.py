"""Strict input validation for everything a caller can send.

Pure functions, no I/O. Each raises `InvalidInput` with a message that
says what to pass instead, because the caller is usually a language model
that will read the error and retry — a message that only says "invalid"
costs a whole round trip to learn nothing.

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
_LIBRARY = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?"       # package name
    r"(\[[A-Za-z0-9._,-]+\])?"                          # optional extras
    r"((==|>=|<=|~=|!=|<|>)[A-Za-z0-9._*+!-]+)?$"       # optional version
)


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


def language(value: object, supported: tuple[str, ...] | None = None) -> str:
    """Validate a language against what the ACTIVE runtime can deliver.

    The supported set is passed in rather than read from policy, because
    the two runtimes differ and an entry in that set is a promise to the
    caller. Defaults to policy.LANGUAGES so existing callers are unchanged.
    """
    allowed = supported if supported is not None else LANGUAGES
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise InvalidInput(
            f"Unsupported language '{value}'. Supported: "
            f"{', '.join(sorted(allowed))}."
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


def libraries(value: object) -> list[str]:
    """Package names only — never flags, URLs, paths or VCS references."""
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
    cleaned: list[str] = []
    for item in names:
        if not isinstance(item, str):
            raise InvalidInput(
                f"Every library must be a string, got "
                f"{type(item).__name__}."
            )
        name = item.strip()
        if not _LIBRARY.match(name):
            raise InvalidInput(
                f"'{item[:64]}' is not an accepted package name. Pass a "
                "plain name with an optional version, like 'requests' or "
                "'pandas==2.2.0'. Installer flags, URLs, file paths and "
                "VCS references are refused: the install step is the only "
                "moment the sandbox has network access, and it is limited "
                "to named packages from the default index."
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
            "Build one with: hyperbox build <name> --custom <Dockerfile>"
        )
    return name
