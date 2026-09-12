"""Acceptance test for the sandbox lifecycle. No mocking — this drives
the real NativeRuntime, which starts a real Docker (or Podman)
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
import time
import uuid

sys.path.insert(0, "src")

from hyperbox_mcp import engine, errors  # noqa: E402
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

    # 4b. Every failure is structured, and structured is now the ONLY
    #     shape.
    #
    #     The caller is usually a model that will read the error and
    #     retry, so a code it can branch on and a fix it can act on are
    #     worth more than prose. 0.3.0 also carried a top-level
    #     `error_message` for clients parsing the pre-0.3 bare string;
    #     that window was one release wide and closed in 0.4.0, so its
    #     absence is asserted rather than assumed — reinstating the
    #     duplicate must fail here.
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
        "errors carry a machine-readable code, and only the structured shape",
        isinstance(payload, dict)
        and payload.get("code") == "INVALID_INPUT"
        and bool(payload.get("message"))
        and "error_message" not in bad,
        f"code={payload.get('code') if isinstance(payload, dict) else payload!r} "
        f"keys={sorted(bad)}",
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

    # 5b was the llm-sandbox output-check recursion guard, and it went
    # with that runtime in 0.4.0. The property it protected is still
    # covered: the native runtime's canary runs on first use, and 5a
    # above proves a sandbox works after a timeout.

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

    # 5e. The RESEAL proof must be able to fail too.
    #
    #     5d covers create(). This covers the only other path that ever
    #     un-seals a live sandbox: run(libraries=[...]), which re-opens
    #     the network mid-session to install and then re-seals. That
    #     reseal used to be trusted rather than proven -- so the one code
    #     path deliberately opening a hole was the one never checking it
    #     had closed. Stub the seal and the sandbox must be destroyed,
    #     not returned describing itself as sealed.
    if hasattr(rt, "_assert_network_sealed"):
        leaky = type(rt)()
        leaky_handle = None
        try:
            leaky_handle = leaky.create(
                language="python", backend=backend, sandbox_id=new_id()
            )
            # Sealed correctly at create; break it only for the reseal.
            leaky._seal = lambda handle: None
            try:
                leaky.run(leaky_handle, "print('unreachable')",
                          libraries=["six"])
                reseal_refused = False
            except errors.NetworkLeakError:
                reseal_refused = True
            except Exception:  # noqa: BLE001 - any other failure is not it
                reseal_refused = False
            gone = not leaky.alive(leaky_handle)
        except Exception as exc:  # noqa: BLE001 - setup failure, not a result
            reseal_refused = gone = False
            print(f"      (setup failed: {exc})")
        finally:
            if leaky_handle is not None:
                try:
                    leaky.destroy(leaky_handle)
                except Exception:  # noqa: BLE001
                    pass
        check(
            "a failed RESEAL after run(libraries=...) is refused",
            reseal_refused,
            "NETWORK_LEAK when the mid-session reseal is stubbed out",
        )
        check(
            "and the leaking sandbox is destroyed, not left running",
            gone,
            "a sandbox that cannot be proven sealed must not survive",
        )
    else:
        skip(
            "a failed RESEAL after run(libraries=...) is refused",
            "this runtime has no seal proof.",
        )

    # 5e2. A RUNNING exec has no exit code, and must not be given one.
    #
    #      The linchpin, tested directly because the path that turns it
    #      into corruption is Windows-only: there, _read_until_done gives
    #      up on an idle stream and the caller then asks for an exit code
    #      the engine does not have. On a unix socket the read raises
    #      first, so this check is the only place the underlying rule is
    #      asserted on this platform.
    #
    #      Verified against a real engine: a running exec reports
    #      `Running: True, ExitCode: None`, and the old `or 0` read that
    #      as a clean success.
    import threading as _threading

    from hyperbox_mcp.rest import api as _api2

    probe_client = rt._client(backend)  # noqa: SLF001 - asserting a primitive
    probe_cid = rt._ref(handle)  # noqa: SLF001
    live_exec = _api2.exec_create(probe_client, probe_cid, ["sleep", "5"])
    _threading.Thread(
        target=lambda: _api2.exec_start(probe_client, live_exec), daemon=True
    ).start()
    time.sleep(1.0)
    try:
        got = _api2.exec_exit_code(probe_client, live_exec)
        invented = f"returned {got!r} for an exec that is still running"
        refused_code = False
    except errors.ExecIncompleteError as exc:
        invented, refused_code = exc.code, True
    except Exception as exc:  # noqa: BLE001
        invented, refused_code = f"wrong error: {type(exc).__name__}", False
    check(
        "a running exec yields no exit code, rather than an invented zero",
        refused_code,
        invented,
    )

    # 5f. An install that outruns its budget must FAIL, and take the
    #     container with it.
    #
    #     This is the one that matters most. `exec_exit_code` used to read
    #     Docker's `ExitCode: null` -- what a RUNNING exec reports -- as
    #     `or 0`, so an install the reader had given up on came back as a
    #     clean success. _provision's `if code != 0` then passed and
    #     create() sealed the sandbox around a half-installed dependency
    #     set, with no network left to repair it. A sandbox described as
    #     provisioned that is not is the exact failure this project
    #     exists to prevent, so the assertion is not merely "an error was
    #     returned" -- it is that NO container survives.
    import hyperbox_mcp.policy as pol
    from hyperbox_mcp import native_runtime as _nr
    from hyperbox_mcp.rest import api as _api

    original_budget = pol.PROVISION_TIMEOUT_SECONDS
    original_install = _nr.LANGUAGES["python"]["install"]
    pol.PROVISION_TIMEOUT_SECONDS = 3.0
    # SILENT, not merely slow, and that distinction is the whole bug. Both
    # read paths give up on an idle stream, so a pip install streaming
    # progress never trips the budget however long it runs -- which is
    # correct. What tripped it in the field was a native wheel
    # (cryptography, asyncpg) compiling: minutes of real work with nothing
    # on stdout. `sleep` reproduces exactly that and nothing else.
    _nr.LANGUAGES["python"]["install"] = ["sh", "-c", "sleep 30; exit 0"]
    starved_id = new_id()
    # A fresh runtime: install clients are cached per instance and carry
    # the budget, so reusing `rt` would reuse the original ceiling.
    starved_rt = type(rt)()
    try:
        starved_rt.create(
            language="python", backend=backend, sandbox_id=starved_id,
            packages=["six"],
        )
        refused, why = False, "create returned a handle"
    except errors.ProvisionError as exc:
        refused, why = True, f"{exc.code}"
    except Exception as exc:  # noqa: BLE001
        refused, why = False, f"wrong error: {type(exc).__name__}: {exc}"
    finally:
        pol.PROVISION_TIMEOUT_SECONDS = original_budget
        _nr.LANGUAGES["python"]["install"] = original_install

    check(
        "an install that outruns its budget is refused, not reported done",
        refused,
        why,
    )

    # The container must be gone, not merely unreported.
    survivors = []
    try:
        client = rt._client(backend)  # noqa: SLF001 - asserting cleanup
        for row in _api.list_managed(client):
            if (row.get("Labels") or {}).get(pol.LABEL_ID) == starved_id:
                survivors.append(row.get("Id", "")[:12])
    except Exception as exc:  # noqa: BLE001
        survivors = [f"(could not ask the engine: {exc})"]
    check(
        "and its container is destroyed, not left sealed and half-built",
        not survivors,
        f"survivors: {survivors}" if survivors else "no container carries that id",
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
        _tools = await client.list_tools()
        create_schema = {
            t.name: getattr(t, "input_schema", None) or t.inputSchema
            for t in _tools
        }["create_sandbox"]
        create_desc = " ".join(next(
            (t.description or "") for t in _tools if t.name == "create_sandbox"
        ).split())
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
        # No built-in environments since 0.4: the only one was an
        # unpinned image in someone else's namespace. Every entry here is
        # something a human built, so the assertion is about SHAPE -- an
        # agent must be able to read it -- not about a name being present.
        check(
            "capabilities lists environments as objects an agent can read",
            isinstance(envs, list) and all(isinstance(e, dict) for e in envs),
            f"{len(env_names)} environment(s): {env_names[:4]}",
        )
        check(
            "each environment names its image, so a choice is not a guess",
            # Vacuously true when there are none, which is the normal
            # state now: run_all isolates HYPERBOX_ENV_DIR, and there are
            # no built-ins. The property is about the SHAPE of an entry.
            isinstance(envs, list) and all(
                isinstance(e, dict) and e.get("name") and e.get("image")
                for e in envs
            ),
            str(envs)[:150],
        )
        # The inbound boundary must be STATED, not left to be inferred.
        # It was documented nowhere -- not in a description, not in
        # capabilities, not in docs -- while 127.0.0.1 appeared three
        # times purely as an affordance that works. An agent read that,
        # reasonably concluded a URL was a real thing to hand back, and a
        # user clicked it and got nothing. Tool descriptions are routing
        # logic, so this is asserted like any other behaviour.
        # Whitespace-normalised: the assertion is that the statement is
        # still THERE, not that it is still wrapped the same way.
        run_desc = " ".join(next(
            (t.description or "") for t in _tools if t.name == "run"
        ).split())
        check(
            "run() states that a sandbox port is unreachable from the host",
            "ONLY FROM INSIDE THIS SANDBOX" in run_desc
            and "Do not hand them a URL" in run_desc,
            "an agent must not have to infer this from 'no route to the "
            "internet', which is the outbound half only",
        )
        check(
            "capabilities states the inbound boundary too",
            "impossible" in (caps.get("network", {})
                             .get("inbound_from_the_host", "")),
            str(caps.get("network", {}).get("inbound_from_the_host"))[:110],
        )
        check(
            "create_sandbox carries the hyperbox build command itself",
            "hyperbox build" in (create_desc or ""),
            "deferring it to a resource hides it from clients that cannot "
            "read resources -- which is where the heavy-install wall is hit",
        )

        check(
            "capabilities names the running runtime",
            caps.get("runtime") == "NativeRuntime",
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

        # A specific code, not just "an error happened": rt.create() raises
        # UnsupportedLanguageError directly (checked earlier in this file),
        # but that bypasses the MCP boundary entirely. validate.language()
        # is what actually runs first on this path, and it used to raise
        # the generic INVALID_INPUT instead -- a real mismatch a live
        # client test caught that this in-process check never could,
        # because it never looked at which code came back.
        unsupported = await client.call_tool(
            "create_sandbox", {"language": "klingon"}
        )
        up = unsupported.data if hasattr(unsupported, "data") else unsupported
        check(
            "an unrecognized language is UNSUPPORTED_LANGUAGE, not INVALID_INPUT",
            isinstance(up, dict)
            and up.get("error", {}).get("code") == "UNSUPPORTED_LANGUAGE",
            str(up)[:120],
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
            and "No sandbox" in (unknown.get("error") or {}).get("message", ""),
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

        # A background run and its log, end to end. Shipped in 0.4.0 with
        # no coverage at all: `get_process_logs` appeared in this file
        # only as a string inside the tool-name assertion above, so
        # nothing had ever called it.
        started = (
            await client.call_tool(
                "run",
                {"sandbox_id": sid, "background": True,
                 "code": (
                     "import time, sys\n"
                     "for i in range(6):\n"
                     "    print('tick', i, flush=True)\n"
                     "    time.sleep(0.5)\n"
                 )},
            )
        ).data
        pid = started.get("process_id") if isinstance(started, dict) else None
        check(
            "a background run returns a process_id instead of output",
            bool(pid) and started.get("status") == "running"
            and "stdout" not in started,
            str(started)[:120],
        )

        if pid:
            # Poll rather than sleep a fixed time: the assertion is that
            # the log GROWS, and a fixed wait either flakes on a slow
            # engine or wastes time on a fast one.
            first_seen, grew = "", False
            for _ in range(20):
                await asyncio.sleep(0.5)
                logs = (
                    await client.call_tool(
                        "get_process_logs",
                        {"sandbox_id": sid, "process_id": pid},
                    )
                ).data
                out = (logs or {}).get("output", "")
                if out and not first_seen:
                    first_seen = out
                elif first_seen and len(out) > len(first_seen):
                    grew = True
                    break
            check(
                "get_process_logs returns output that grows as it runs",
                grew,
                f"first={first_seen!r} (a background run must keep writing)",
            )

            # Reading a log counts as use -- otherwise an agent polling a
            # long job watches its sandbox expire underneath it.
            before = server._registry.get(sid)
            await asyncio.sleep(1.1)
            await client.call_tool(
                "get_process_logs", {"sandbox_id": sid, "process_id": pid}
            )
            after = server._registry.get(sid)
            check(
                "reading logs counts as use, so polling holds off the TTL",
                bool(before and after and after.expires_at > before.expires_at),
                f"expires_at {getattr(before, 'expires_at', None)} -> "
                f"{getattr(after, 'expires_at', None)}",
            )

            bad = (
                await client.call_tool(
                    "get_process_logs",
                    {"sandbox_id": sid, "process_id": "0" * 32},
                )
            ).data
            check(
                "an unknown process_id is an error, not empty output",
                isinstance(bad, dict) and "error" in bad,
                str(bad)[:110],
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
