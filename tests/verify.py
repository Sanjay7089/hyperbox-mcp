"""Acceptance test for the sandbox lifecycle. No mocking — this drives
the real LLMSandboxRuntime, which starts a real Docker (or Podman)
container. Requires Docker or Podman actually installed and running.

    python tests/verify.py [docker|podman] [language]

Language defaults to python. Every language in the runtime's map is
expected to clear the SAME bar — that is the point: an entry in
_LANGUAGES is a promise the tool can deliver that environment, so it is
added only after this suite passes for it. See REQUIREMENTS.md Phase 2.

Prints PASS/FAIL per case and exits non-zero on any failure. This is
what the verify-hyperbox-mcp skill runs.

IMPORTANT — failure triage (see DESIGN.md): if create_sandbox itself
errors, first decide WHICH layer failed before touching code:
  - Is Docker/Podman actually running on this machine?
  - Is the backend socket reachable (DOCKER_HOST / CONTAINER_HOST)?
    On macOS the podman socket is inside the VM; export the host-side
    path from `podman machine inspect` or podman-py cannot connect.
  - Is the base image pullable from here? A first run of a compiled
    language pulls a large toolchain image and can take minutes.
A create failure is very often the environment, not runtime.py. Don't
edit the implementation until you've ruled the environment out.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from hyperbox_mcp import llm_sandbox_runtime as lsr  # noqa: E402
from hyperbox_mcp import server  # noqa: E402
from hyperbox_mcp.runtime import Runtime  # noqa: E402

MARKER = "hello from hyperbox-mcp"
STATE_FILE = "/tmp/persisted.txt"

# One snippet set per language. `libraries`/`lib_use` are optional — the
# package-persistence case is skipped for languages where installing a
# dependency mid-session isn't a meaningful operation.
SNIPPETS: dict[str, dict] = {
    "python": {
        "hello": f"print('{MARKER}')",
        "write": f"open('{STATE_FILE}', 'w').write('42')",
        "read": f"print(open('{STATE_FILE}').read())",
        "broken": "raise ValueError('deliberately broken')",
        "spin": "while True: pass",
        "libraries": ["six"],
        "lib_use": "import six; print('six', six.__version__)",
        "lib_marker": "six",
    },
    "javascript": {
        "hello": f"console.log('{MARKER}');",
        "write": f"require('fs').writeFileSync('{STATE_FILE}', '42');",
        "read": f"console.log(require('fs').readFileSync('{STATE_FILE}', 'utf8'));",
        "broken": "throw new Error('deliberately broken');",
        "spin": "while (true) {}",
    },
    "ruby": {
        "hello": f"puts '{MARKER}'",
        "write": f"File.write('{STATE_FILE}', '42')",
        "read": f"puts File.read('{STATE_FILE}')",
        "broken": "raise 'deliberately broken'",
        "spin": "loop do end",
    },
    "go": {
        "hello": 'package main\nimport "fmt"\nfunc main() { fmt.Println("' + MARKER + '") }',
        "write": 'package main\nimport "os"\nfunc main() { os.WriteFile("'
        + STATE_FILE
        + '", []byte("42"), 0644) }',
        "read": 'package main\nimport ("fmt"; "os")\nfunc main() { b, _ := os.ReadFile("'
        + STATE_FILE
        + '"); fmt.Println(string(b)) }',
        "broken": 'package main\nfunc main() { panic("deliberately broken") }',
        "spin": "package main\nfunc main() { for {} }",
    },
    "java": {
        "hello": 'public class Main { public static void main(String[] a) {'
        ' System.out.println("' + MARKER + '"); } }',
        "write": "import java.nio.file.*;\npublic class Main { public static void"
        ' main(String[] a) throws Exception { Files.write(Paths.get("'
        + STATE_FILE
        + '"), "42".getBytes()); } }',
        "read": "import java.nio.file.*;\npublic class Main { public static void"
        ' main(String[] a) throws Exception { System.out.println(new'
        ' String(Files.readAllBytes(Paths.get("' + STATE_FILE + '")))); } }',
        "broken": "public class Main { public static void main(String[] a) {"
        ' throw new RuntimeException("deliberately broken"); } }',
        "spin": "public class Main { public static void main(String[] a) {"
        " while (true) {} } }",
    },
    "cpp": {
        "hello": '#include <iostream>\nint main() { std::cout << "'
        + MARKER
        + '" << std::endl; }',
        "write": '#include <fstream>\nint main() { std::ofstream f("'
        + STATE_FILE
        + '"); f << "42"; }',
        "read": '#include <iostream>\n#include <fstream>\n#include <string>\n'
        'int main() { std::ifstream f("' + STATE_FILE + '"); std::string s;'
        " f >> s; std::cout << s << std::endl; }",
        "broken": "#include <stdexcept>\nint main() { throw"
        ' std::runtime_error("deliberately broken"); }',
        "spin": "int main() { while (true) {} }",
    },
    "r": {
        "hello": f'cat("{MARKER}\\n")',
        "write": f'writeLines("42", "{STATE_FILE}")',
        "read": f'cat(readLines("{STATE_FILE}"), "\\n")',
        "broken": 'stop("deliberately broken")',
        "spin": "while (TRUE) {}",
    },
}

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")


def summarize() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "docker"
    language = sys.argv[2] if len(sys.argv) > 2 else "python"
    if language not in SNIPPETS:
        print(f"No snippet set for '{language}'. Known: {', '.join(SNIPPETS)}")
        return 1
    snip = SNIPPETS[language]
    print(f"--- {language} on {backend} ---")

    rt: Runtime = lsr.LLMSandboxRuntime()

    # 0. Invalid inputs fail fast, before any container is created.
    try:
        rt.create(language="cobol", backend=backend)
        check("invalid language raises before container creation", False)
    except lsr.UnsupportedLanguageError:
        check("invalid language raises before container creation", True)

    # 1. Create a persistent sandbox.
    try:
        handle = rt.create(language=language, backend=backend)
    except Exception as exc:  # noqa: BLE001 - surface env failures clearly
        check(
            "create_sandbox succeeds",
            False,
            f"{type(exc).__name__}: {exc}  <-- is {backend} running? see triage note in this file",
        )
        return summarize()

    check("create_sandbox returns an id", bool(handle.sandbox_id), handle.sandbox_id)

    # 2. Passing run — real stdout, success True.
    ok = rt.run(handle, snip["hello"])
    check(
        "passing run: success + expected stdout",
        ok.success and MARKER in ok.stdout,
        str(ok),
    )

    # 3. The SANDBOX persists across runs — filesystem and installed
    #    packages, which is what the build→run→fix→re-run loop needs.
    #    Not interpreter memory: each run() is a fresh process. See
    #    DESIGN.md's Decision Log, 2026-09-04.
    rt.run(handle, snip["write"])
    from_file = rt.run(handle, snip["read"])
    check(
        "filesystem persists across run() calls",
        from_file.success and "42" in from_file.stdout,
        str(from_file),
    )

    if "lib_use" in snip:
        rt.run(handle, snip["lib_use"], libraries=snip["libraries"])
        installed = rt.run(handle, snip["lib_use"])
        check(
            "installed package persists into a later run that omits libraries",
            installed.success and snip["lib_marker"] in installed.stdout,
            str(installed),
        )

    # 4. Broken run — non-zero exit, real error text, success False.
    broken = rt.run(handle, snip["broken"])
    check(
        "broken run: not success + real error in stderr",
        (not broken.success) and "deliberately broken" in broken.stderr,
        str(broken),
    )

    # 5. Timeout — a structured failure, not a crash and not a silent
    #    success. timed_out must actually be set, or the field is a lie.
    timed = rt.run(handle, snip["spin"], timeout=5)
    check(
        "timeout: not success + timed_out set + reason in stderr",
        (not timed.success) and timed.timed_out and "Timeout" in timed.stderr,
        str(timed),
    )

    # 6. Destroy, then destroy again — idempotent.
    rt.destroy(handle)
    rt.destroy(handle)  # must not raise
    check("destroy is idempotent (second call does not raise)", True)

    # 7. The MCP tool layer end to end, driven through a real client so
    #    Context injection and the declared surface are exercised too —
    #    calling the functions directly would skip both.
    asyncio.run(_check_tool_layer(language, backend))

    return summarize()


async def _check_tool_layer(language: str, backend: str) -> None:
    from fastmcp import Client

    async with Client(server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
        check("tool surface is exactly the three lifecycle tools",
              names == {"create_sandbox", "run", "destroy_sandbox"},
              str(sorted(names)))

        # The comprehension layer: an agent must be able to discover the
        # limits without first crashing into them.
        resources = [str(r.uri) for r in await client.list_resources()]
        check("capabilities resource is published",
              "hyperbox://capabilities" in resources, str(resources))
        prompts = [p.name for p in await client.list_prompts()]
        check("run_safely prompt is published", "run_safely" in prompts, str(prompts))

        by_name = {t.name: t for t in await client.list_tools()}
        ann = by_name["destroy_sandbox"].annotations
        check("destroy_sandbox is annotated destructive + idempotent",
              bool(ann and ann.destructiveHint and ann.idempotentHint),
              str(ann))
        ann_run = by_name["run"].annotations
        check("run is annotated NOT host-destructive",
              bool(ann_run and ann_run.destructiveHint is False),
              str(ann_run))

        made = (await client.call_tool(
            "create_sandbox", {"language": language, "backend": backend})).data
        if not isinstance(made, dict) or "sandbox_id" not in made:
            check("tool-level destroy reports a status", False, str(made))
            return
        sid = made["sandbox_id"]
        first = (await client.call_tool("destroy_sandbox", {"sandbox_id": sid})).data
        second = (await client.call_tool("destroy_sandbox", {"sandbox_id": sid})).data
        check(
            "destroy tool: 'destroyed' then 'already_gone', no false boolean",
            first.get("status") == "destroyed"
            and second.get("status") == "already_gone"
            and False not in first.values()
            and False not in second.values(),
            f"first={first} second={second}",
        )


if __name__ == "__main__":
    sys.exit(main())
