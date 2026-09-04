"""Acceptance test for the sandbox lifecycle. No mocking — this drives
the real LLMSandboxRuntime, which starts a real Docker (or Podman)
container. Requires Docker or Podman actually installed and running.

    python tests/verify.py [docker|podman]

Prints PASS/FAIL per case and exits non-zero on any failure. This is
what the verify-sandbox-mcp skill runs.

IMPORTANT — failure triage (see DESIGN.md): if create_sandbox itself
errors, first decide WHICH layer failed before touching code:
  - Is Docker/Podman actually running on this machine?
  - Is the backend socket reachable (DOCKER_HOST)?
  - Is the base image pullable from here?
A create failure is very often the environment, not runtime.py. Don't
edit the implementation until you've ruled the environment out.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from sandbox_mcp import llm_sandbox_runtime as lsr  # noqa: E402
from sandbox_mcp import server  # noqa: E402
from sandbox_mcp.runtime import Runtime  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "docker"
    rt: Runtime = lsr.LLMSandboxRuntime()

    # 0. Invalid inputs fail fast, before any container is created.
    try:
        rt.create(language="cobol", backend=backend)
        check("invalid language raises before container creation", False)
    except lsr.UnsupportedLanguageError:
        check("invalid language raises before container creation", True)

    # 1. Create a persistent sandbox.
    try:
        handle = rt.create(language="python", backend=backend)
        created = True
    except Exception as exc:  # noqa: BLE001 - surface env failures clearly
        created = False
        check(
            "create_sandbox succeeds",
            False,
            f"{type(exc).__name__}: {exc}  <-- is {backend} running? see triage note in this file",
        )

    if not created:
        print(f"\n{sum(1 for r in results if r[1])}/{len(results)} passed")
        return 1

    check("create_sandbox returns an id", bool(handle.sandbox_id), handle.sandbox_id)

    # 2. Passing run — real stdout, success True.
    ok = rt.run(handle, "print('hello from sandbox-mcp')")
    check(
        "passing run: success + expected stdout",
        ok.success and "hello from sandbox-mcp" in ok.stdout,
        str(ok),
    )

    # 3. The SANDBOX persists across runs — filesystem and installed
    #    packages, which is what the build→run→fix→re-run loop needs.
    #    Not interpreter memory: each run() is a fresh process. See
    #    DESIGN.md's Decision Log, 2026-09-04.
    rt.run(handle, "open('/tmp/persisted.txt', 'w').write('42')")
    from_file = rt.run(handle, "print(open('/tmp/persisted.txt').read())")
    check(
        "filesystem persists across run() calls",
        from_file.success and "42" in from_file.stdout,
        str(from_file),
    )

    rt.run(handle, "import six", libraries=["six"])
    installed = rt.run(handle, "import six; print('six', six.__version__)")
    check(
        "installed package persists into a later run that omits libraries",
        installed.success and "six" in installed.stdout,
        str(installed),
    )

    # 4. Broken run — non-zero exit, real traceback, success False.
    broken = rt.run(handle, "raise ValueError('deliberately broken')")
    check(
        "broken run: not success + real traceback",
        (not broken.success) and "ValueError" in broken.stderr,
        str(broken),
    )

    # 5. Timeout — a structured failure, not a crash and not a silent
    #    success. timed_out must actually be set, or the field is a lie.
    timed = rt.run(handle, "while True: pass", timeout=2)
    check(
        "timeout: not success + timed_out set + reason in stderr",
        (not timed.success)
        and timed.timed_out
        and "Timeout" in timed.stderr,
        str(timed),
    )

    # 6. Destroy, then destroy again — idempotent.
    rt.destroy(handle)
    rt.destroy(handle)  # must not raise
    check("destroy is idempotent (second call does not raise)", True)

    # 7. The destroy TOOL reports an affirmative status on both paths.
    #    Nothing else here exercises the MCP tool layer, and a success
    #    that reports a false boolean invites an agent to retry it.
    created = server.create_sandbox.fn(language="python", backend=backend)
    if "error" in created:
        check("tool-level destroy reports a status", False, str(created))
    else:
        first = server.destroy_sandbox.fn(created["sandbox_id"])
        second = server.destroy_sandbox.fn(created["sandbox_id"])
        check(
            "destroy tool: 'destroyed' then 'already_gone', no false boolean",
            first.get("status") == "destroyed"
            and second.get("status") == "already_gone"
            and False not in first.values()
            and False not in second.values(),
            f"first={first} second={second}",
        )

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
