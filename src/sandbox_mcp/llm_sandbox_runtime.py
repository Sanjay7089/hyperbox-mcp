"""The llm-sandbox implementation of the Runtime protocol.

THIS IS THE ONLY FILE ALLOWED TO IMPORT llm_sandbox. Every name and
signature below was verified directly against the installed package
(not assumed from docs) — see DESIGN.md's decision log:

- create_session(backend=SandboxBackend.X, lang=SupportedLanguage.Y)
  returns a session with explicit .open() / .close() (verified present),
  so we can hold it open across many .run() calls — a persistent
  sandbox, not a one-shot context manager.
- .run(code, libraries=..., timeout=...) -> ConsoleOutput with
  .stdout / .stderr / .exit_code.
- Top-level exceptions actually exported: SandboxError (base),
  ContainerError, ResourceError, SecurityError, ValidationError.
  (MissingDependencyError is NOT top-level — do not import it.)
- SandboxTimeoutError is NOT top-level either; it lives in
  llm_sandbox.exceptions and subclasses SandboxError, so it must be
  caught BEFORE the generic backend tuple or it disappears into it.
- Verified against a real container: each .run() is a fresh process in
  the SAME container (differing os.getpid(), identical nodename). The
  filesystem and pip-installed packages persist between runs;
  interpreter memory does not.
- There is NO SupportedLanguage for bash/shell. Not our concern here.
"""

from __future__ import annotations

import uuid

from llm_sandbox import (
    ContainerError,
    ResourceError,
    SandboxBackend,
    SandboxError,
    SecurityError,
    SupportedLanguage,
    ValidationError,
    create_session,
)
from llm_sandbox.exceptions import SandboxTimeoutError

from sandbox_mcp.runtime import ExecResult, SandboxHandle

# These maps hold ONLY what has actually been run against a real
# container. javascript (and java/cpp/go/ruby/r) are free from
# llm-sandbox, and podman is Phase 2 — but an entry here is a promise
# the tool can deliver that environment, so nothing is added until a
# real verified run passes. See REQUIREMENTS.md Phase 1.
_LANGUAGES = {
    "python": SupportedLanguage.PYTHON,
}

_BACKENDS = {
    "docker": SandboxBackend.DOCKER,
}

_BACKEND_EXCEPTIONS = (
    SandboxError,
    ContainerError,
    ResourceError,
    SecurityError,
    ValidationError,
)


class UnsupportedLanguageError(ValueError):
    pass


class UnsupportedBackendError(ValueError):
    pass


class SandboxRuntimeError(RuntimeError):
    """Wraps any backend exception so callers never see raw llm-sandbox
    types — keeps the backend replaceable."""


class LLMSandboxRuntime:
    """Runtime implementation backed by llm-sandbox.

    Holds open sessions in-process, keyed by sandbox_id, so a sandbox
    persists across multiple run() calls until destroy().
    """

    def __init__(self) -> None:
        self._sessions: dict[str, object] = {}

    def create(self, language: str, backend: str) -> SandboxHandle:
        if language not in _LANGUAGES:
            raise UnsupportedLanguageError(
                f"Unsupported language '{language}'. Supported: "
                f"{', '.join(_LANGUAGES)}"
            )
        if backend not in _BACKENDS:
            raise UnsupportedBackendError(
                f"Unsupported backend '{backend}'. Supported: "
                f"{', '.join(_BACKENDS)}"
            )

        try:
            session = create_session(
                backend=_BACKENDS[backend], lang=_LANGUAGES[language]
            )
            session.open()
        except _BACKEND_EXCEPTIONS as exc:
            raise SandboxRuntimeError(f"{type(exc).__name__}: {exc}") from exc

        sandbox_id = uuid.uuid4().hex[:12]
        self._sessions[sandbox_id] = session
        return SandboxHandle(
            sandbox_id=sandbox_id, language=language, backend=backend
        )

    def run(
        self,
        handle: SandboxHandle,
        code: str,
        libraries: list[str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        session = self._sessions.get(handle.sandbox_id)
        if session is None:
            raise SandboxRuntimeError(
                f"No live sandbox '{handle.sandbox_id}'. "
                "Was it destroyed, or never created?"
            )
        try:
            out = session.run(code, libraries=libraries, timeout=timeout)
        except SandboxTimeoutError as exc:
            # Caught before _BACKEND_EXCEPTIONS, which would otherwise
            # swallow it (it subclasses SandboxError). A timeout is a
            # distinct outcome: the code hung rather than failed, and
            # the agent's next move differs accordingly.
            return ExecResult(
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                exit_code=-1,
                timed_out=True,
            )
        except _BACKEND_EXCEPTIONS as exc:
            # A backend error running code is a structured failure the
            # agent should reason about, not a crash — return it as one.
            return ExecResult(
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
                exit_code=-1,
            )
        return ExecResult(
            stdout=out.stdout,
            stderr=out.stderr,
            exit_code=out.exit_code,
        )

    def destroy(self, handle: SandboxHandle) -> None:
        session = self._sessions.pop(handle.sandbox_id, None)
        if session is None:
            return  # idempotent: already gone
        try:
            session.close()
        except _BACKEND_EXCEPTIONS as exc:
            raise SandboxRuntimeError(f"{type(exc).__name__}: {exc}") from exc
