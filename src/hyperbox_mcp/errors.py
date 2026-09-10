"""One error type per thing that can actually go wrong, and what to do next.

Every failure a caller sees carries four things:

    code     machine-readable, so an agent can branch without parsing prose
    message  what happened
    fix      the command or change that resolves it, where one exists
    context  which engine, which transport, which container

The `fix` field is not politeness. The caller is usually a language model
that will read the error and retry, so an error that only says what broke
costs a whole round trip to learn nothing. The same is true of a person at
a terminal, who otherwise goes looking for the answer in the source.

**The one distinction that must never collapse.** `ContainerGoneError` means
the engine answered and the container is not there. `EngineUnavailableError`
means the engine could not be asked at all. Reporting the second as the
first is how orphaned containers accumulate: the caller drops its record
while the container keeps running. That is why they are separate types
rather than one type with a flag, and why no handler may catch a base class
in order to treat them alike.

Each class keeps a builtin base as well — LookupError, RuntimeError,
ValueError — so existing `except` clauses go on working while the codebase
migrates onto these. Removing those bases is a breaking change to callers,
not a tidy-up.
"""

from __future__ import annotations

from typing import Any


class HyperBoxError(Exception):
    """Base for everything this project raises deliberately.

    Never raised directly: catching it is for a boundary that must not
    crash, such as a tool handler turning any failure into a structured
    result.
    """

    #: Machine-readable, stable, and part of the public surface. Renaming
    #: one breaks callers that branch on it.
    code = "HYPERBOX_ERROR"

    def __init__(
        self,
        message: str,
        fix: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.context = dict(context or {})

    def as_dict(self) -> dict[str, Any]:
        """The shape a tool result carries. Empty fields are omitted so a
        caller can test presence rather than emptiness."""
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        if self.context:
            out["context"] = self.context
        return out

    def __str__(self) -> str:
        return f"{self.message} {self.fix}".strip()


# --- the engine, and reaching it -------------------------------------


class EngineError(HyperBoxError):
    code = "ENGINE_ERROR"


class EngineUnavailableError(EngineError, RuntimeError):
    """The container engine could not be reached at all.

    Never means "the container is gone" — that is ContainerGoneError.
    """

    code = "ENGINE_NOT_RUNNING"


class NoEngineError(EngineError):
    """Neither Docker nor Podman is installed, so there is nowhere to run
    code. Distinct from ENGINE_NOT_RUNNING because the fix differs: one
    is `start it`, the other is `install one`."""

    code = "NO_ENGINE_INSTALLED"


class EngineRefusedError(EngineError):
    """The engine answered, and the answer was an error.

    Distinct from EngineUnavailableError, and the distinction is the same
    one ContainerGoneError draws: a reply is an answer. An engine that
    says "no such repository" or "denied" is healthy and reachable, and
    telling the caller ENGINE_NOT_RUNNING sends them to restart something
    that is already running -- the one action that cannot help.
    """

    code = "ENGINE_REFUSED"


class SocketBusyError(EngineError):
    """Too many HyperBox processes are driving the engine at once.

    Transient by nature, so the message says how many were active — a
    number a person can act on, unlike "try again".
    """

    code = "SOCKET_BUSY"


class ContainerGoneError(EngineError, LookupError):
    """The engine answered and confirmed this container does not exist.

    A 404 from a live engine is an answer. A refused connection is not,
    and must never be reported here.
    """

    code = "CONTAINER_GONE"


# --- the sandbox, once it exists -------------------------------------


class SandboxError(HyperBoxError):
    code = "SANDBOX_ERROR"


class ProvisionError(SandboxError):
    """Setup failed while the sandbox was being built. The container is
    destroyed rather than handed back half-provisioned."""

    code = "PROVISION_FAILED"


class ExecutionTimeoutError(SandboxError):
    """Submitted code outlived its timeout and was killed."""

    code = "EXECUTION_TIMEOUT"


class OOMKilledError(SandboxError):
    """The kernel killed it for exceeding the memory ceiling. Reported
    separately because a bare SIGKILL tells a reasoning agent nothing."""

    code = "OOM_KILLED"


class NetworkLeakError(SandboxError):
    """The sandbox could still reach the network after sealing.

    Fatal, and never downgraded to a warning: a sandbox described as
    sealed while connected is the worst outcome this project has.
    """

    code = "NETWORK_LEAK"


class PolicyNotAppliedError(SandboxError):
    """The engine accepted the resource policy and applied something
    else. The container is destroyed rather than described as limited."""

    code = "POLICY_NOT_APPLIED"


class SandboxStaleError(SandboxError):
    """The session is no longer usable and must be reattached.

    Its own type because the caller's next move is specific — reattach,
    not recreate and not give up — and a generic runtime error leaves it
    guessing between the three.
    """

    code = "SANDBOX_STALE"


# --- what a caller asked for -----------------------------------------


class ConfigError(HyperBoxError):
    code = "CONFIG_ERROR"


class InvalidInput(ConfigError, ValueError):
    """A caller-supplied value the server refuses to act on."""

    code = "INVALID_INPUT"


class UnsupportedLanguageError(ConfigError):
    code = "UNSUPPORTED_LANGUAGE"


class UnsupportedBackendError(ConfigError):
    code = "UNSUPPORTED_BACKEND"


class UnknownEnvironmentError(ConfigError):
    """The named environment does not exist. Distinct from a language
    error so the agent is told to build one, not to pick another
    language."""

    code = "UNKNOWN_ENVIRONMENT"


def to_result(exc: BaseException) -> dict[str, Any]:
    """Render any exception as a tool result's error payload.

    One shape, and only one: `{"error": {code, message, fix, context}}`.
    0.3.0 also carried the message as a top-level `error_message` string,
    so a client parsing the pre-0.3 bare-string shape kept working; that
    migration window was one release wide and closed in 0.4.0. Two fields
    saying the same thing is exactly what structured errors were meant to
    end — the caller branches on `code`, not on prose.
    """
    if isinstance(exc, HyperBoxError):
        payload = exc.as_dict()
    else:
        payload = {
            "code": "UNEXPECTED",
            "message": f"{type(exc).__name__}: {exc}",
        }
    return {"error": payload}
