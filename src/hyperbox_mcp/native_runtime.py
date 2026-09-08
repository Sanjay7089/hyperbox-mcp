"""A Runtime built on the REST driver, with no execution backend beneath it.

The second implementation of the Runtime protocol, and the one that exists
because the first could not be fixed from outside. Two defects drove it:

  - llm-sandbox's timeout stops nothing. Its cancellation is a host-side
    thread join; the container-level kill its documentation promises is a
    no-op for a container the session created and a bare detach for one it
    reattached to. Submitted code went on consuming the sandbox's entire
    CPU ceiling after `run` had already returned.
  - Adding languages meant adopting unpinned `:latest` images from one
    personal namespace, a C++ handler that shells `apt-get install` with no
    `-y`, and an R handler that interpolates package names into an
    `install.packages(...)` string.

What it does NOT re-derive is everything in sandbox_ops: the policy
read-back, sealing, the OOM explanation, orphan collection. Those are
shared with the llm-sandbox runtime rather than written twice, so both
implementations are covered by one suite.

Execution here is deliberately explicit at three points where the backend
was implicit:

  - the container is kept alive by a command WE choose, not by whatever
    CMD the image happens to carry;
  - submitted code is written to policy.CODE_DIR, never under a tmpfs,
    because the archive API cannot write through one on Docker and returns
    200 having done nothing;
  - a timeout kills the process GROUP, so anything the code forked dies
    with it, and the kill is verified rather than assumed.
"""

from __future__ import annotations

import shlex
import threading
import uuid
from typing import Any

from hyperbox_mcp import errors, policy, sandbox_ops
from hyperbox_mcp.rest import api
from hyperbox_mcp.rest.client import EngineClient
from hyperbox_mcp.runtime import ExecResult, SandboxHandle

#: Kept alive by a command we choose. Relying on the image's own CMD makes
#: the sandbox's lifetime a property of whichever image was selected.
KEEPALIVE = ["sleep", "infinity"]

#: How a language is run and where its code lands.
LANGUAGES: dict[str, dict[str, Any]] = {
    "python": {
        "image": "ghcr.io/vndee/sandbox-python-311-bullseye:latest",
        "extension": "py",
        "argv": ["python3"],
        "canary": "print('{marker}')",
    },
}

CANARY_MARKER = "__hyperbox_canary__"


class NativeRuntime:
    """Runtime protocol over the REST driver."""

    def __init__(self) -> None:
        self._clients: dict[str, EngineClient] = {}
        self._verified: set[str] = set()

    # --- plumbing -----------------------------------------------------

    def _client(self, backend: str) -> EngineClient:
        existing = self._clients.get(backend)
        if existing is None:
            from hyperbox_mcp import engine

            existing = EngineClient(engine.endpoint_for(backend))
            self._clients[backend] = existing
        return existing

    def _spec(self, language: str) -> dict:
        spec = LANGUAGES.get(language)
        if spec is None:
            raise errors.UnsupportedLanguageError(
                f"Unsupported language '{language}'.",
                fix=f"Supported: {', '.join(sorted(LANGUAGES))}.",
            )
        return spec

    @staticmethod
    def _ref(handle: SandboxHandle) -> str:
        ref = handle.meta.get("container_ref")
        if not ref:
            raise errors.ContainerGoneError(
                f"Sandbox '{handle.sandbox_id}' has no container reference."
            )
        return ref

    def _container_getter(self, handle: SandboxHandle):
        """sandbox_ops works with objects exposing `.attrs`; the REST API
        returns plain dicts. One adapter, rather than teaching the shared
        code about two shapes."""
        client = self._client(handle.backend)
        ref = self._ref(handle)

        def get():
            attrs = api.inspect_container(client, ref)
            return type("RestContainer", (), {"attrs": attrs, "id": ref,
                                              "reload": lambda self: None})()

        return get

    def _seal(self, handle: SandboxHandle) -> None:
        client = self._client(handle.backend)
        cid = self._ref(handle)

        def attached() -> list[str]:
            attrs = api.inspect_container(client, cid)
            settings = attrs.get("NetworkSettings") or {}
            return list((settings.get("Networks") or {}).keys())

        def disconnect(name: str) -> None:
            api.disconnect_network(client, name, cid)

        sandbox_ops.seal(attached, disconnect, handle.sandbox_id)

    def _unseal(self, handle: SandboxHandle) -> None:
        client = self._client(handle.backend)
        cid = self._ref(handle)
        sandbox_ops.unseal(
            handle.backend,
            lambda: [n.get("Name", "") for n in api.list_networks(client)],
            lambda name: api.connect_network(client, name, cid),
        )

    def running_code_pids(self, handle: SandboxHandle) -> list[str]:
        """PIDs inside the sandbox still running submitted code.

        Part of the Runtime surface rather than a private helper, because
        it is what proves a timeout actually stopped something — and a
        check that cannot be asked of both runtimes cannot compare them.
        """
        return self._running_code(self._client(handle.backend), self._ref(handle))

    def image_for(self, language: str, environment: str | None = None) -> str:
        """The image a sandbox would start from. Used to warn about a pull
        before one begins, so a cold start reads as a download rather than
        a hang."""
        if environment:
            return policy.environments().get(environment, "")
        return self._spec(language)["image"]

    # --- Runtime protocol ---------------------------------------------

    def create(
        self, language: str, backend: str, sandbox_id: str,
        environment: str | None = None,
    ) -> SandboxHandle:
        spec = self._spec(language)
        client = self._client(backend)

        image = spec["image"]
        if environment:
            available = policy.environments()
            if environment not in available:
                raise errors.UnknownEnvironmentError(
                    f"Unknown environment '{environment}'.",
                    fix=f"Available: {', '.join(sorted(available))}. Build "
                        "one with: hyperbox build <name> --dockerfile <path>",
                )
            image = available[environment]

        cid = api.create_container(client, image, sandbox_id, KEEPALIVE)
        handle = SandboxHandle(
            sandbox_id=sandbox_id, language=language, backend=backend,
            meta={"container_ref": cid},
        )
        # Past this point everything either succeeds or takes the container
        # with it. A half-configured sandbox is never handed back.
        try:
            api.start_container(client, cid)
            sandbox_ops.assert_policy_applied(
                api.inspect_container(client, cid), sandbox_id
            )
            api.run_exec(client, cid, ["mkdir", "-p", policy.CODE_DIR])
            self._seal(handle)
        except BaseException:
            try:
                api.remove_container(client, cid)
            except Exception:  # noqa: BLE001 - already failing
                pass
            raise
        return handle

    def run(
        self, handle: SandboxHandle, code: str,
        libraries: list[str] | None = None, timeout: float | None = None,
    ) -> ExecResult:
        client = self._client(handle.backend)
        cid = self._ref(handle)
        self._verify_once(handle)

        if libraries:
            failure = self._install(handle, client, cid, libraries, timeout)
            if failure is not None:
                return failure

        return self._exec_with_deadline(handle, client, cid, code, timeout)

    def _install(self, handle, client, cid, libraries, timeout):
        """Install dependencies with the network briefly attached.

        The caller's code is not run here, and the reseal is unconditional:
        a failed install must still leave the sandbox sealed.
        """
        try:
            self._unseal(handle)
            code, out, err = api.run_exec(
                client, cid,
                ["python3", "-m", "pip", "install", "--no-input", *libraries],
            )
        except Exception as exc:  # noqa: BLE001
            return ExecResult(
                stdout="", stderr=f"Dependency install failed: {exc}", exit_code=-1
            )
        finally:
            self._seal(handle)
        if code != 0:
            return ExecResult(
                stdout=out, stderr=f"Dependency install failed:\n{err}",
                exit_code=code,
            )
        return None

    def _exec_with_deadline(self, handle, client, cid, code, timeout):
        """Run the code, and stop it for real if it outlives its timeout."""
        spec = self._spec(handle.language)
        run_id = uuid.uuid4().hex
        path = f"{policy.CODE_DIR}/{run_id}.{spec['extension']}"
        pid_file = f"{policy.CODE_DIR}/{run_id}.pid"
        api.put_file(client, cid, path, code.encode())

        # `exec` replaces the shell, so the pid recorded IS the
        # interpreter's and it leads its own process group. Shell builtins
        # only -- nothing here depends on a binary the image may not ship.
        argv = " ".join(shlex.quote(part) for part in [*spec["argv"], path])
        wrapped = ["sh", "-c", f"echo $$ > {pid_file}; exec {argv}"]

        result: dict[str, Any] = {}

        def work():
            try:
                exec_id = api.exec_create(client, cid, wrapped)
                out, err = api.exec_start(client, exec_id)
                result.update(
                    code=api.exec_exit_code(client, exec_id), out=out, err=err
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(timeout)

        if worker.is_alive():
            note = self._kill(client, cid, pid_file)
            return ExecResult(
                stdout="", stderr=f"Timed out after {timeout:g}s.{note}",
                exit_code=-1, timed_out=True,
            )
        if "error" in result:
            raise result["error"]

        stderr = result.get("err", "")
        if result.get("code") == 137 and not stderr.strip():
            stderr = sandbox_ops.explain_sigkill(self._container_getter(handle))
        return ExecResult(
            stdout=result.get("out", ""), stderr=stderr,
            exit_code=result.get("code", -1),
        )

    def _kill(self, client, cid, pid_file: str) -> str:
        """Kill the run's process GROUP, and verify it actually died.

        The negative pid is the point: `kill -9 <pid>` leaves anything the
        code forked running as an orphan under PID 1, still consuming the
        sandbox's CPU. `kill -9 -<pid>` takes the group.
        """
        try:
            code, _, _ = api.run_exec(
                client, cid,
                ["sh", "-c",
                 f'p=$(cat {pid_file} 2>/dev/null); [ -n "$p" ] && '
                 f'{{ kill -9 -"$p" 2>/dev/null || kill -9 "$p" 2>/dev/null; }}; '
                 f'rm -f {pid_file}; exit 0'],
            )
        except Exception as exc:  # noqa: BLE001 - already reporting a timeout
            return (f" The sandbox could not be reached to stop it ({exc}); "
                    "call destroy_sandbox to be certain.")
        survivors = self._running_code(client, cid)
        if survivors:
            try:
                api.restart_container(client, cid)
                return (" It would not die, so the sandbox was restarted; "
                        f"scratch space ({', '.join(policy.TMPFS_PATHS)}) is "
                        "now empty.")
            except Exception as exc:  # noqa: BLE001
                return (f" It could not be stopped ({exc}); call "
                        "destroy_sandbox.")
        return " The code was killed; the sandbox is still usable."

    def _running_code(self, client, cid) -> list[str]:
        """Processes still running submitted code, read from /proc.

        Not `ps` or `pkill`: those live in procps, which the slim language
        images do not ship, so a check depending on them would report
        'nothing running' by failing to look.
        """
        probe = (
            "import os\n"
            "me=os.getpid()\n"
            "hits=[]\n"
            "for e in os.listdir('/proc'):\n"
            "    if not e.isdigit() or int(e)==me: continue\n"
            "    try: c=open('/proc/'+e+'/cmdline','rb').read().decode('utf8','replace')\n"
            "    except OSError: continue\n"
            f"    if '{policy.CODE_DIR}/' in c: hits.append(e)\n"
            "print(' '.join(hits))\n"
        )
        try:
            code, out, _ = api.run_exec(client, cid, ["python3", "-c", probe])
        except Exception:  # noqa: BLE001
            return []
        return [t for t in out.split() if t.isdigit()] if code == 0 else []

    def _verify_once(self, handle: SandboxHandle) -> None:
        """Prove results reach us, once per sandbox per process.

        The id is marked BEFORE the check runs, not after: the check calls
        run(), which calls this, and marking afterwards recurses until the
        stack gives out. That was a real failure; the ordering is its fix.
        """
        if handle.sandbox_id in self._verified:
            return
        self._verified.add(handle.sandbox_id)
        try:
            spec = self._spec(handle.language)
            probe = spec["canary"].format(marker=CANARY_MARKER)
            out = self.run(handle, probe, timeout=30)
            if CANARY_MARKER not in out.stdout:
                raise errors.SandboxStaleError(
                    f"Sandbox '{handle.sandbox_id}' on {handle.backend} started "
                    "but its output does not reach this server: a startup "
                    f"check printed nothing (exit {out.exit_code}, "
                    f"stdout={out.stdout!r}). Every run would report success "
                    "with empty output, so it is refused.",
                    fix="Use a different backend, or report this.",
                    context={"sandbox_id": handle.sandbox_id},
                )
        except BaseException:
            self._verified.discard(handle.sandbox_id)
            raise

    def alive(self, handle: SandboxHandle) -> bool:
        try:
            attrs = api.inspect_container(
                self._client(handle.backend), self._ref(handle)
            )
        except errors.ContainerGoneError:
            return False
        return (attrs.get("State") or {}).get("Running", False)

    def destroy(self, handle: SandboxHandle) -> None:
        """Returns only on confirmed absence. An unreachable engine raises,
        so the caller keeps its record rather than forgetting a container
        that may still be running."""
        self._verified.discard(handle.sandbox_id)
        try:
            api.remove_container(self._client(handle.backend), self._ref(handle))
        except errors.ContainerGoneError:
            return

    def gc(self, known_ids: set[str]) -> list[str]:
        return sandbox_ops.collect_orphans(known_ids, ("docker", "podman"))
