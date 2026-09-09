"""Acceptance test for the sandbox lifecycle. No mocking — this drives
the real LLMSandboxRuntime, which starts a real Docker (or Podman)
container. Requires a container engine actually installed and running.

    python tests/verify.py [docker|podman] [language]

Language defaults to python, which is the only entry in the runtime's map
for v0.1. The other snippet sets are kept because every language is
expected to clear the SAME bar — an entry in the map is a promise the
tool can deliver that environment, so one is added only after this suite
passes for it against a real container.

Prints PASS/FAIL per case and exits non-zero on any failure.

IMPORTANT — failure triage: if create_sandbox itself errors, decide WHICH
layer failed before touching code:
  - Is the container engine actually running? `hyperbox doctor` answers
    this, and names the fix.
  - Is the base image pullable from here? A first run pulls a large image
    and can take minutes.
A create failure is very often the environment, not the runtime. Don't
edit the implementation until you've ruled the environment out.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

sys.path.insert(0, "src")

from hyperbox_mcp import engine, errors  # noqa: E402
from hyperbox_mcp import llm_sandbox_runtime as lsr  # noqa: E402
from hyperbox_mcp import server  # noqa: E402
from hyperbox_mcp.runtime import Runtime  # noqa: E402

MARKER = "hello from hyperbox"
STATE_FILE = "/work/persisted.txt"

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
    # --- not in the runtime's map yet; kept for re-promotion ----------
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
    "bash": {
        "hello": f"echo '{MARKER}'",
        "write": f"echo 42 > {STATE_FILE}",
        "read": f"cat {STATE_FILE}",
        "broken": "echo 'deliberately broken' >&2; exit 1",
        "spin": "while true; do :; done",
    },
    "java": {
        "hello": 'public class Main { public static void main(String[] a) '
                 '{ System.out.println("' + MARKER + '"); } }',
        "write": 'import java.nio.file.*;\npublic class Main { public static '
                 'void main(String[] a) throws Exception { Files.write('
                 'Paths.get("' + STATE_FILE + '"), "42".getBytes()); } }',
        "read": 'import java.nio.file.*;\npublic class Main { public static '
                'void main(String[] a) throws Exception { System.out.println('
                'new String(Files.readAllBytes(Paths.get("' + STATE_FILE + '")))); } }',
        "broken": 'public class Main { public static void main(String[] a) '
                  '{ throw new RuntimeException("deliberately broken"); } }',
        "spin": 'public class Main { public static void main(String[] a) '
                '{ while (true) {} } }',
    },
    "go": {
        "hello": 'package main\nimport "fmt"\nfunc main() { fmt.Println("'
        + MARKER
        + '") }',
        "write": 'package main\nimport "os"\nfunc main() { os.WriteFile("'
        + STATE_FILE
        + '", []byte("42"), 0644) }',
        "read": 'package main\nimport ("fmt"; "os")\nfunc main() { b, _ := os.ReadFile("'
        + STATE_FILE
        + '"); fmt.Println(string(b)) }',
        "broken": 'package main\nfunc main() { panic("deliberately broken") }',
        "spin": "package main\nfunc main() { for {} }",
    },
}

results: list[tuple[str, bool, str]] = []


def skip(name: str, reason: str) -> None:
    """A case that cannot run here. Never counted as a pass."""
    print(f"SKIP  {name}", flush=True)
    print(f"        {reason}")


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}", flush=True)


def summarize() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "docker"
    language = sys.argv[2] if len(sys.argv) > 2 else "python"
    if language not in SNIPPETS:
        print(f"No snippet set for '{language}'. Known: {', '.join(SNIPPETS)}")
        return 1
    snip = SNIPPETS[language]
    print(f"--- {language} on {backend} ---")

    # Whichever runtime is configured, so one suite covers both. The
    # point of the overlap release is that the SAME acceptance bar is
    # applied to each; a suite hardcoded to one of them cannot do that.
    rt: Runtime = server.select_runtime()
    print(f"--- runtime: {type(rt).__name__} ---", flush=True)

    # 0. Invalid inputs fail fast, before any container is created.
    try:
        rt.create(language="cobol", backend=backend, sandbox_id=new_id())
        check("invalid language raises before container creation", False)
    except errors.UnsupportedLanguageError:
        check("invalid language raises before container creation", True)

    # 1. Create a persistent sandbox.
    try:
        handle = rt.create(
            language=language, backend=backend, sandbox_id=new_id()
        )
    except Exception as exc:  # noqa: BLE001 - surface env failures clearly
        check(
            "create_sandbox succeeds",
            False,
            f"{type(exc).__name__}: {exc}  <-- run `hyperbox doctor` first",
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
    #    packages, which is what the build->run->fix->re-run loop needs.
    #    Not interpreter memory: each run() is a fresh process.
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

    # 4b. Every failure is structured, and still readable by a v0.2 client.
    #
    #     The caller is usually a model that will read the error and
    #     retry, so a code it can branch on and a fix it can act on are
    #     worth more than prose. `error_message` carries the old bare
    #     string for one release, because every client parsing that shape
    #     would otherwise break on the day this changed.
    # FastMCP may hand back the plain function or a wrapper; take
    # whichever is callable rather than assuming.
    run_fn = getattr(server.run, "fn", server.run)

    class _Ctx:
        """The Context a real client injects. run() reports progress on it
        so a long call is not silence; here nothing is listening."""

        async def report_progress(self, *a, **k):
            return None

    def call_run(**kwargs):
        return asyncio.run(run_fn(_Ctx(), **kwargs))

    bad = call_run(sandbox_id="../etc/passwd", code="x")
    payload = bad.get("error")
    check(
        "errors carry a machine-readable code and a compat string",
        isinstance(payload, dict)
        and payload.get("code") == "INVALID_INPUT"
        and bad.get("error_message") == payload.get("message"),
        f"code={payload.get('code') if isinstance(payload, dict) else payload!r}",
    )
    gone = call_run(sandbox_id="000000000000", code="print(1)")
    gone_payload = gone.get("error", {})
    check(
        "an unusable sandbox says so with a code and a next step",
        gone_payload.get("code") == "SANDBOX_STALE" and bool(gone_payload.get("fix")),
        f"code={gone_payload.get('code')} fix={gone_payload.get('fix', '')[:48]!r}",
    )

    # 5. Timeout — a structured failure, not a crash and not a silent
    #    success. timed_out must actually be set, or the field is a lie.
    timed = rt.run(handle, snip["spin"], timeout=5)
    # The property, not one runtime's phrasing. This asserted the literal
    # word "Timeout", which happened to appear in llm-sandbox's exception
    # name and not in the native runtime's plainer "Timed out after 5s" --
    # so a correct implementation failed a test that was measuring
    # vocabulary.
    check(
        "timeout: not success + timed_out set + reason in stderr",
        (not timed.success)
        and timed.timed_out
        and ("timed out" in timed.stderr.lower() or "timeout" in timed.stderr.lower()),
        str(timed),
    )

    # 5a. And the timeout has to actually STOP it.
    #
    #     Returning a timeout while the code keeps running is the failure
    #     this replaced: the backend's own timeout is a host-side
    #     thread.join, and the container-level cancellation its docstring
    #     promises is a no-op. `while True: pass` went on consuming the
    #     sandbox's whole CPU ceiling until the sandbox was destroyed, and
    #     nothing in this suite noticed, because asserting that the CALL
    #     returned says nothing about whether the WORK stopped.
    #
    #     Read from /proc with the interpreter that is PID 1 in these
    #     images: `ps` lives in procps, which the slim images do not ship,
    #     so a check that shells out to it would pass by finding nothing
    #     for the wrong reason.
    survivors = ""
    try:
        survivors = " ".join(rt.running_code_pids(handle))
    except Exception as exc:  # noqa: BLE001
        survivors = f"could not ask the container: {exc}"
    check(
        "timeout: the code is actually dead, not merely abandoned",
        survivors == "",
        f"submitted-code PIDs still running: {survivors or 'none'}",
    )

    # 5a-ii. Killing it must not have unsealed it. The fallback path
    #        restarts the container, and a restart that silently restored
    #        the default network would hand back a sandbox described as
    #        sealed and connected to the internet.
    # Asked of the engine, not of either runtime's bookkeeping, so the
    # answer means the same thing whichever one is under test.
    container = engine.get_container(handle.backend, handle.meta["container_ref"])
    container.reload()
    attached = list(
        ((container.attrs.get("NetworkSettings") or {}).get("Networks") or {})
    )
    check(
        "timeout: the sandbox is still sealed afterwards",
        attached == [],
        f"networks attached after the timeout: {attached or 'none'}",
    )

    # 5a-iii. And still usable — a timeout must cost the run, not the
    #         sandbox.
    after = rt.run(handle, snip["hello"], timeout=30)
    check(
        "timeout: the sandbox still works afterwards",
        after.success and MARKER in after.stdout,
        str(after),
    )

    # 5b. The startup output check runs ONCE per sandbox and does not
    #     recurse. It verifies output by calling run(), which opens a
    #     session, which is what triggers the check — so a guard that is
    #     set after the call instead of before it recurses until the
    #     stack gives out. That was a real failure; this is its test.
    probe = lsr.LLMSandboxRuntime()  # this case inspects llm-sandbox internals
    calls = {"n": 0}
    original = probe._assert_results_round_trip

    def counted(h):
        calls["n"] += 1
        return original(h)

    probe._assert_results_round_trip = counted
    probe_handle = None
    try:
        # Always python: this case is about the output check not
        # recursing, which is a property of the guard rather than of any
        # language, and llm-sandbox supports only python anyway.
        probe_handle = probe.create(
            language="python", backend=backend, sandbox_id=new_id()
        )
        check(
            "creating a sandbox does not pay for the output check",
            calls["n"] == 0,
            f"round-trip checks during create: {calls['n']}",
        )
        probe_code = SNIPPETS["python"]["hello"]
        first = probe.run(probe_handle, probe_code)
        after_first = calls["n"]
        probe.run(probe_handle, probe_code)
        check(
            "the output check runs exactly once, on first use, without recursing",
            after_first == 1 and calls["n"] == 1 and first.success,
            f"checks after first run={after_first}, after second={calls['n']}",
        )
    except RecursionError as exc:
        check(
            "the output check runs exactly once, on first use, without recursing",
            False,
            f"RecursionError: {exc}",
        )
    finally:
        if probe_handle is not None:
            probe.destroy(probe_handle)

    # 5c. A dropped connection is not an outage. Engines close idle
    #     sockets — Docker Desktop on Windows does it within seconds —
    #     and a cached client keeps the dead one. Treating that as an
    #     unreachable engine made destroy() refuse to confirm removal and
    #     leave the container running.
    from hyperbox_mcp import engine as eng
    from hyperbox_mcp.engine import EngineUnavailableError

    dropped = EngineUnavailableError(
        "Could not reach docker (ConnectionError: ('Connection aborted.', "
        "RemoteDisconnected('Remote end closed connection without response')))"
    )
    down = EngineUnavailableError(
        "Docker is not reachable (DockerException: FileNotFoundError(2))"
    )
    check(
        "a dropped socket is retried, a real outage is not",
        eng.is_stale_connection(dropped) and not eng.is_stale_connection(down),
        f"dropped={eng.is_stale_connection(dropped)} "
        f"down={eng.is_stale_connection(down)}",
    )

    # 5d. The seal proof must be able to FAIL.
    #
    #     A guard that has never been seen to reject anything is a
    #     hypothesis. Stub the seal to a no-op and creation must refuse:
    #     a sandbox described as isolated while it can reach the internet
    #     is the worst outcome available here, so it is destroyed rather
    #     than returned with a warning.
    if hasattr(rt, "_assert_network_sealed"):
        unsealed = type(rt)()
        unsealed._seal = lambda handle: None
        leaked_handle = None
        try:
            leaked_handle = unsealed.create(
                language="python", backend=backend, sandbox_id=new_id()
            )
            refused = False
        except errors.NetworkLeakError:
            refused = True
        except Exception:  # noqa: BLE001 - any other failure is not the point
            refused = False
        finally:
            if leaked_handle is not None:
                try:
                    unsealed.destroy(leaked_handle)
                except Exception:  # noqa: BLE001
                    pass
        check(
            "an unsealed sandbox is refused, not handed back",
            refused,
            "NETWORK_LEAK when the seal is stubbed out",
        )
    else:
        skip(
            "an unsealed sandbox is refused, not handed back",
            "this runtime has no create-time seal proof.",
        )

    # 6. Destroy, then destroy again — idempotent.
    rt.destroy(handle)
    rt.destroy(handle)  # must not raise
    check("destroy is idempotent (second call does not raise)", True)
    check("destroyed sandbox reports not alive", not rt.alive(handle))

    # 7. The MCP tool layer end to end, driven through a real client so
    #    Context injection and the declared surface are exercised too —
    #    calling the functions directly would skip both.
    asyncio.run(_check_tool_layer(language, backend))

    return summarize()


async def _check_tool_layer(language: str, backend: str) -> None:
    from fastmcp import Client

    async with Client(server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
        # Four, and the count is asserted rather than a floor: an MCP
        # client routes on tool name and description alone, so every tool
        # added is another thing it can pick wrongly. The fourth exists
        # because a background run has no moment to hand output back --
        # reading it later is the only way -- and `build` is still kept
        # OUT, because it runs arbitrary commands on the host.
        check(
            "tool surface is exactly the four tools an agent may call",
            names == {"create_sandbox", "run", "destroy_sandbox",
                      "get_process_logs"},
            str(sorted(names)),
        )

        # create_sandbox exposes environment, and it is the ONLY way an
        # agent touches environments: building one runs arbitrary RUN
        # commands as root with network access, so it stays a CLI action.
        create_schema = {
            t.name: getattr(t, "input_schema", None) or t.inputSchema
            for t in await client.list_tools()
        }["create_sandbox"]
        check(
            "create_sandbox accepts an environment",
            "environment" in (create_schema.get("properties") or {}),
            str(sorted((create_schema.get("properties") or {}))),
        )
        check(
            "there is no tool that builds an environment",
            not any("build" in n for n in names),
            str(sorted(names)),
        )

        # The comprehension layer: an agent must be able to discover the
        # limits without first crashing into them.
        resources = [str(r.uri) for r in await client.list_resources()]
        check(
            "capabilities resource is published",
            "hyperbox://capabilities" in resources,
            str(resources),
        )
        prompts = [p.name for p in await client.list_prompts()]
        check("run_safely prompt is published", "run_safely" in prompts, str(prompts))

        caps = json.loads(
            (await client.read_resource("hyperbox://capabilities"))[0].text
        )
        envs = caps.get("environments")
        env_names = [
            e.get("name") for e in envs if isinstance(e, dict)
        ] if isinstance(envs, list) else []
        check(
            "capabilities lists the environments an agent may pick",
            "python" in env_names,
            str(envs)[:150],
        )
        check(
            "each environment names its image, so a choice is not a guess",
            bool(envs) and all(
                isinstance(e, dict) and e.get("name") and e.get("image")
                for e in envs
            ),
            str(envs)[:150],
        )
        check(
            "capabilities names the running runtime",
            caps.get("runtime") in ("NativeRuntime", "LLMSandboxRuntime"),
            str(caps.get("runtime")),
        )
        # An agent that needs an environment must be able to learn the
        # command a HUMAN runs to make one, without failing first.
        actions = caps.get("host_actions") or {}
        check(
            "capabilities names the host commands an agent must ask for",
            "hyperbox build" in (actions.get("create_environment") or ""),
            str(actions.get("create_environment")),
        )

        by_name = {t.name: t for t in await client.list_tools()}
        ann = by_name["destroy_sandbox"].annotations
        check(
            "destroy_sandbox is annotated destructive + idempotent",
            bool(ann and ann.destructive_hint and ann.idempotent_hint),
            str(ann),
        )
        ann_run = by_name["run"].annotations
        check(
            "run is annotated NOT host-destructive",
            bool(ann_run and ann_run.destructive_hint is False),
            str(ann_run),
        )

        # --- strict input validation, at the tool boundary -------------
        for label, env in (
            ("path traversal", "../../etc"),
            ("an unknown name", "definitely-not-built"),
            ("a name with a space", "has space"),
        ):
            res = await client.call_tool(
                "create_sandbox", {"language": language, "environment": env}
            )
            payload = res.data if hasattr(res, "data") else res
            check(
                f"create_sandbox refuses {label} as an environment",
                isinstance(payload, dict) and "error" in payload,
                str(payload)[:110],
            )

        bad_inputs = {
            "negative timeout is rejected": {
                "sandbox_id": "a" * 12, "code": "print(1)", "timeout": -5},
            "zero timeout is rejected": {
                "sandbox_id": "a" * 12, "code": "print(1)", "timeout": 0},
            "null timeout is rejected": {
                "sandbox_id": "a" * 12, "code": "print(1)", "timeout": None},
            "malformed sandbox_id is rejected": {
                "sandbox_id": "../../etc/passwd", "code": "print(1)"},
            "empty code is rejected": {"sandbox_id": "a" * 12, "code": "   "},
            "installer flag as a library is rejected": {
                "sandbox_id": "a" * 12, "code": "print(1)",
                "libraries": ["--index-url http://example.invalid/"]},
            "VCS reference as a library is rejected": {
                "sandbox_id": "a" * 12, "code": "print(1)",
                "libraries": ["git+https://example.invalid/x.git"]},
        }
        for name, args in bad_inputs.items():
            try:
                out = (await client.call_tool("run", args)).data
                rejected = isinstance(out, dict) and "error" in out
                detail = str(out)[:90]
            except Exception as exc:  # noqa: BLE001 - schema refusal counts
                rejected, detail = True, f"{type(exc).__name__} at the schema"
            check(name, rejected, detail)

        # NaN and infinity cannot survive JSON transport — they arrive as
        # null — so the tool-level cases above cannot reach them. Assert
        # the guard where it actually applies, since `min(nan, 60)`
        # returns nan and would otherwise become a timeout that never
        # fires.
        from hyperbox_mcp import validate

        for label, value in (("NaN", float("nan")), ("infinity", float("inf"))):
            try:
                validate.timeout(value)
                check(f"{label} timeout is rejected by the validator", False)
            except validate.InvalidInput as exc:
                check(
                    f"{label} timeout is rejected by the validator",
                    True,
                    str(exc)[:70],
                )

        # A valid-but-unknown id is a clean miss, not a crash.
        unknown = (
            await client.call_tool(
                "run", {"sandbox_id": "0" * 12, "code": "print(1)"}
            )
        ).data
        check(
            "unknown sandbox_id returns a readable error",
            "error" in unknown
            and "No sandbox" in unknown.get("error_message", ""),
            str(unknown)[:90],
        )

        made = (
            await client.call_tool(
                "create_sandbox", {"language": language, "backend": backend}
            )
        ).data
        if not isinstance(made, dict) or "sandbox_id" not in made:
            check("tool-level destroy reports a status", False, str(made))
            return
        sid = made["sandbox_id"]

        # Broken code must not crash the server; it must be reportable.
        crashed = (
            await client.call_tool(
                "run", {"sandbox_id": sid, "code": "raise SystemExit(3)"}
            )
        ).data
        check(
            "code that exits hard is reported, not crashed on",
            isinstance(crashed, dict) and "exit_code" in crashed,
            str(crashed)[:110],
        )

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
