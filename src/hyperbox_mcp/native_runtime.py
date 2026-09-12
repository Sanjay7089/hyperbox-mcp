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
in sandbox_ops rather than inline, so a future backend inherits the
behaviour rather than a description of it.

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
from pathlib import Path as PathType
from typing import Any

from hyperbox_mcp import buildcontext, errors, policy, sandbox_ops
from hyperbox_mcp.rest import api
from hyperbox_mcp.rest.client import EngineClient
from hyperbox_mcp.runtime import ExecResult, SandboxHandle

#: Kept alive by a command we choose. Relying on the image's own CMD makes
#: the sandbox's lifetime a property of whichever image was selected.
KEEPALIVE = ["sleep", "infinity"]

#: How each language is run, and how packages reach it.
#:
#: Official images, tagged explicitly. llm-sandbox's defaults were seven
#: untagged images from one individual's GHCR namespace — mutable `latest`
#: for a tool whose whole claim is containment. `install` of None means the
#: language takes no packages, and asking for some is refused at create
#: time rather than failing halfway through provisioning.
#:
#: An entry here is a promise the tool can deliver that language, so
#: nothing joins policy.LANGUAGES until the suite passes for it against a
#: real container.
LANGUAGES: dict[str, dict[str, Any]] = {
    "python": {
        "image": "python:3.12-slim",
        "extension": "py",
        "argv": ["python3"],
        "canary": "print('MARKER')",
        "install": ["python3", "-m", "pip", "install", "--no-input"],
    },
    "javascript": {
        "image": "node:22-slim",
        "extension": "js",
        "argv": ["node"],
        "canary": "console.log('MARKER');",
        "install": ["npm", "install", "--global", "--no-fund", "--no-audit"],
    },
    "bash": {
        "image": "debian:bookworm-slim",
        "extension": "sh",
        "argv": ["bash"],
        "canary": "echo 'MARKER'",
        # apt needs its index refreshed first; see _install.
        "install": ["apt-get", "install", "-y", "--no-install-recommends"],
        "install_prelude": ["apt-get", "update", "-qq"],
    },
    "go": {
        "image": "golang:1.23-bookworm",
        "extension": "go",
        "argv": ["go", "run"],
        "canary": 'package main\nimport "fmt"\nfunc main() { fmt.Println("MARKER") }',
        "install": ["go", "get"],
        "setup": [["go", "mod", "init", "sandbox"]],
    },
    "java": {
        "image": "eclipse-temurin:21-jdk",
        "extension": "java",
        "argv": ["java"],
        "canary": 'public class Main { public static void main(String[] a)'
                  ' { System.out.println("MARKER"); } }',
        "filename": "Main",
        # Single-file source execution. Dependency management is Maven or
        # Gradle, which is a build system rather than a package install,
        # so it is refused rather than half-supported.
        "install": None,
    },
}

CANARY_MARKER = "__hyperbox_canary__"


class NativeRuntime:
    """Runtime protocol over the REST driver."""

    def __init__(self) -> None:
        self._clients: dict[str, EngineClient] = {}
        # Separate, because installs run on a different budget. See
        # _install_client.
        self._install_clients: dict[str, EngineClient] = {}
        self._verified: set[str] = set()

    # --- plumbing -----------------------------------------------------

    def _client(self, backend: str) -> EngineClient:
        existing = self._clients.get(backend)
        if existing is None:
            from hyperbox_mcp import engine

            # Explicit, not the default: the socket must outlive the
            # longest run it carries, or the two timeouts race.
            existing = EngineClient(
                engine.endpoint_for(backend),
                timeout=policy.ENGINE_SOCKET_TIMEOUT,
            )
            self._clients[backend] = existing
        return existing

    def _install_client(self, backend: str) -> EngineClient:
        """A client for dependency installs, which are not agent code.

        Its own connection, on its own budget: the shared one is sized
        from MAX_TIMEOUT_SECONDS, which bounds what an AGENT submits, and
        a native wheel that compiles for ten minutes is not that. Sharing
        the client meant an install inherited a 180s ceiling nobody had
        measured it against. `builder.py` already keeps a long-budget
        client of its own for image builds, for the same reason.
        """
        existing = self._install_clients.get(backend)
        if existing is None:
            from hyperbox_mcp import engine

            existing = EngineClient(
                engine.endpoint_for(backend),
                timeout=policy.PROVISION_TIMEOUT_SECONDS,
            )
            self._install_clients[backend] = existing
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

    def _sync_in(self, client, cid, source) -> dict:
        """Copy a host directory into the sandbox, and say what arrived.

        Into CODE_DIR, never /work: the archive API cannot write through
        a tmpfs mount on Docker -- it returns 200 having put the files in
        the layer the mount hides.

        The manifest goes back to the caller because a file that does not
        arrive is, from inside the sandbox, indistinguishable from one the
        agent never wrote. Told which files were skipped and why, it can
        act; left to guess, it invents a reason.
        """
        blob, manifest = buildcontext.sync_tar(
            source,
            ignore_file=policy.SYNC_IGNORE_FILE,
            deny_files=policy.SYNC_DENYLIST,
            deny_dirs=policy.SYNC_DENY_DIRS,
            max_bytes=policy.SYNC_MAX_BYTES,
            max_files=policy.SYNC_MAX_FILES,
            env_opt_in=policy.SYNC_ENV_OPT_IN,
        )
        api.put_tree(client, cid, policy.CODE_DIR, blob)
        manifest["from"] = str(source)
        manifest["to"] = policy.CODE_DIR
        return manifest

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

    def supported_languages(self) -> tuple[str, ...]:
        return tuple(sorted(LANGUAGES))

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
        packages: list[str] | None = None,
        sync_from: "PathType | None" = None,
    ) -> SandboxHandle:
        """Create a sandbox, provision it, then sever its network for good.

        The order is the whole design. Packages are installed while the
        network is attached and BEFORE the caller has run anything, so the
        one moment a sandbox can reach the internet is a moment it is
        executing nothing the caller wrote. After that the network is
        detached and PROVEN detached; from then on there is no window at
        all.
        """
        spec = self._spec(language)
        client = self._client(backend)
        if packages and not spec.get("install"):
            raise errors.InvalidInput(
                f"{language} sandboxes cannot install packages.",
                fix="Build an environment with the dependencies baked in: "
                    "hyperbox build <name> --dockerfile <path>, then pass "
                    "environment=<name>.",
                context={"language": language},
            )

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

        if not api.image_present(client, image) and image.startswith(
            policy.LOCAL_IMAGE_PREFIX
        ):
            # A locally built environment. `hyperbox build` tags both
            # --dockerfile and --image results under this prefix, so no
            # registry has ever heard of it and pulling can only fail --
            # with "pull access denied ... may require 'docker login'",
            # which sends the user looking for a credentials problem that
            # does not exist. Say what actually happened instead.
            raise errors.ProvisionError(
                f"Environment '{environment}' is registered but its image "
                f"({image}) is not on this engine. It was built locally, so "
                "there is nowhere to pull it from.",
                fix="Rebuild it at your terminal: hyperbox build "
                    f"{environment} --image <ref>   (or --dockerfile <path>). "
                    "`hyperbox envs` lists what is registered.",
                context={"environment": environment, "image": image},
            )

        if not api.image_present(client, image):
            # Pull rather than fail. The engine's own progress events are
            # consumed and discarded here — the caller is an agent waiting
            # on a tool call, and the server reports liveness separately;
            # the CLI renders these properly.
            last = {}
            for event in api.pull_image(client, image):
                if "error" in event:
                    raise errors.ProvisionError(
                        f"Could not pull {image}: {event['error']}",
                        fix="Check the image name and network access to its "
                            "registry.",
                        context={"image": image},
                    )
                last = event
            if not api.image_present(client, image):
                raise errors.ProvisionError(
                    f"Pulled {image} but the engine does not have it "
                    f"({last.get('status', 'no final status')}).",
                    context={"image": image},
                )

        cid = api.create_container(client, image, sandbox_id, KEEPALIVE)
        handle = SandboxHandle(
            sandbox_id=sandbox_id, language=language, backend=backend,
            meta={"container_ref": cid},
        )
        # Past this point everything either succeeds or takes the container
        # with it. A half-configured sandbox is never handed back.
        try:
            api.start_container(client, cid)
            attrs = api.inspect_container(client, cid)
            sandbox_ops.assert_policy_applied(attrs, sandbox_id)
            self._prepare_code_dir(client, cid, attrs)
            if sync_from is not None:
                handle.meta["sync"] = self._sync_in(client, cid, sync_from)
            for step in spec.get("setup", []):
                code, out, err = api.run_exec(client, cid, step)
                if code != 0:
                    raise errors.ProvisionError(
                        f"Language setup step {' '.join(step)!r} failed "
                        f"({code}): {(err or out).strip()[:200]}",
                        context={"sandbox_id": sandbox_id, "step": step},
                    )
            if packages:
                self._provision(handle, client, cid, spec, packages)
            # Unconditional since 0.4.0. An environment used to be able to
            # keep its network (`hyperbox build --allow-network`), which
            # made "sealed" a claim every caller had to qualify. There is
            # no exception left to describe: every sandbox is sealed here,
            # and proven sealed on the next line or destroyed.
            self._seal(handle)
            self._assert_network_sealed(handle, client, cid)
        except BaseException:
            try:
                api.remove_container(client, cid)
            except Exception:  # noqa: BLE001 - already failing
                pass
            raise
        return handle

    def _prepare_code_dir(self, client, cid, attrs) -> None:
        """Create CODE_DIR, and make sure the image's own user owns it.

        `mkdir -p /sandbox` used to run as whatever the image's USER is,
        with its exit code discarded. For an image that ends in `USER
        someone` -- which is what a hardened production Dockerfile looks
        like -- that fails at the filesystem root, and the engines then
        diverge in two different wrong directions. Measured on both:

          docker: the archive upload 404s, so create fails, blaming a
                  missing file rather than the permission that caused it.
          podman: the archive API creates /sandbox itself, as root:root
                  0755, and create SUCCEEDS -- handing back a sandbox
                  whose own user cannot write to it, so every run() fails
                  later on a sandbox already reported ready.

        So: create it as root, then hand it to the image's user. Agent
        code still runs as that user -- the hardening the Dockerfile asked
        for is preserved -- it simply has a directory it can write to.
        """
        code, out, err = api.run_exec(
            client, cid, ["mkdir", "-p", policy.CODE_DIR], user="root"
        )
        if code != 0:
            raise errors.ProvisionError(
                f"Could not create {policy.CODE_DIR} in the container "
                f"({code}): {(err or out).strip()[:200]}",
                fix="The image may forbid writing at the filesystem root "
                    "even for root. Pre-create the directory in your "
                    f"Dockerfile: RUN mkdir -p {policy.CODE_DIR}",
                context={"path": policy.CODE_DIR},
            )

        image_user = str((attrs.get("Config") or {}).get("User") or "").strip()
        if not image_user:
            return  # runs as root already; nothing to hand over
        code, out, err = api.run_exec(
            client, cid,
            ["chown", "-R", image_user, policy.CODE_DIR], user="root",
        )
        if code != 0:
            raise errors.ProvisionError(
                f"Could not give {policy.CODE_DIR} to the image's user "
                f"'{image_user}' ({code}): {(err or out).strip()[:200]}",
                fix=f"Pre-create it in your Dockerfile instead: RUN mkdir -p "
                    f"{policy.CODE_DIR} && chown {image_user} "
                    f"{policy.CODE_DIR}",
                context={"path": policy.CODE_DIR, "user": image_user},
            )

    def _provision(self, handle, client, cid, spec, packages) -> None:
        """Install declared packages while the network is still attached.

        Runs on the install client, not the one passed in: see
        _install_client. An install that outlives its budget raises here
        rather than being mistaken for a finished one -- the caller
        destroys the container, which is the only safe outcome, because
        the alternative is sealing a sandbox around a half-installed
        dependency set it can never repair.
        """
        client = self._install_client(handle.backend)
        try:
            for prelude in (
                [spec["install_prelude"]] if spec.get("install_prelude") else []
            ):
                api.run_exec(client, cid, prelude)
            code, out, err = api.run_exec(
                client, cid, [*spec["install"], *packages]
            )
        except (TimeoutError, errors.ExecIncompleteError) as exc:
            raise errors.ProvisionError(
                f"Installing {', '.join(packages)} did not finish within "
                f"{policy.PROVISION_TIMEOUT_SECONDS:g}s, so this sandbox was "
                "destroyed rather than handed back half-provisioned.",
                fix="Bake the dependencies into an environment instead, "
                    "which installs them once: hyperbox build <name> "
                    "--dockerfile <path>, then pass environment=<name>.",
                context={"sandbox_id": handle.sandbox_id, "packages": packages,
                         "budget_seconds": policy.PROVISION_TIMEOUT_SECONDS},
            ) from exc
        if code != 0:
            raise errors.ProvisionError(
                f"Could not install {', '.join(packages)}: "
                f"{(err or out).strip()[:400]}",
                fix="Check the package names, or build an environment with "
                    "them baked in.",
                context={"sandbox_id": handle.sandbox_id, "packages": packages},
            )

    #: Probes that must FAIL once a sandbox is sealed.
    #:
    #: Two of them, because detaching a network does not necessarily remove
    #: the resolver the container inherited from it: /etc/resolv.conf
    #: survives, so name resolution can keep working — or keep hanging —
    #: after every route is gone. A TCP check alone would call that sealed.
    _SEAL_PROBES = (
        ("a TCP connection",
         "import socket,sys\n"
         "try:\n"
         "    socket.setdefaulttimeout(3)\n"
         "    socket.create_connection(('1.1.1.1', 443), 3).close()\n"
         "    print('REACHED')\n"
         "except Exception:\n"
         "    print('blocked')\n"),
        ("a DNS lookup",
         "import socket\n"
         "try:\n"
         "    socket.setdefaulttimeout(3)\n"
         "    socket.getaddrinfo('example.com', 80)\n"
         "    print('REACHED')\n"
         "except Exception:\n"
         "    print('blocked')\n"),
    )

    #: The same two checks in shell, for images with no Python.
    #:
    #: /dev/tcp is a bash builtin, so it needs no binaries at all; getent
    #: comes from libc and is present wherever a resolver is. debian-slim
    #: ships neither python3 nor curl, so an interpreter-only probe would
    #: fail to run and — read carelessly — look like a pass.
    _SHELL_PROBES = (
        ("a TCP connection",
         ["bash", "-c",
          "timeout 3 bash -c 'cat < /dev/tcp/1.1.1.1/443' >/dev/null 2>&1 "
          "&& echo REACHED || echo blocked"]),
        ("a DNS lookup",
         ["bash", "-c",
          "timeout 3 getent hosts example.com >/dev/null 2>&1 "
          "&& echo REACHED || echo blocked"]),
    )

    def _seal_probes(self, client, cid) -> tuple:
        """Whichever probe pair this image can actually run.

        Chosen by asking the container, not by assuming from the language:
        a custom environment may be built on anything.
        """
        try:
            code, _, _ = api.run_exec(client, cid, ["python3", "-c", "pass"])
        except Exception:  # noqa: BLE001
            code = 1
        if code == 0:
            return tuple(
                (what, ["python3", "-c", body]) for what, body in self._SEAL_PROBES
            )
        return self._SHELL_PROBES

    def _assert_network_sealed(self, handle, client, cid) -> None:
        """Prove the seal, from inside, before handing the sandbox back.

        Sealing that reports success and leaves a route open is the worst
        failure available here: the agent is told it is isolated and it is
        not. So the claim is tested rather than asserted, and a sandbox
        that fails is destroyed rather than returned with a warning.

        Needs a Python interpreter, which every base image here has. A
        language whose image lacks one must supply its own probe before it
        can be promoted.
        """
        for what, probe in self._seal_probes(client, cid):
            try:
                code, out, _ = api.run_exec(client, cid, probe)
            except Exception:  # noqa: BLE001 - cannot verify is not sealed
                raise errors.NetworkLeakError(
                    f"Could not verify the network seal on "
                    f"'{handle.sandbox_id}' ({what}).",
                    fix="Destroy this sandbox; it cannot be shown to be "
                        "isolated.",
                    context={"sandbox_id": handle.sandbox_id},
                ) from None
            if "REACHED" in out:
                raise errors.NetworkLeakError(
                    f"Sandbox '{handle.sandbox_id}' still reaches the network "
                    f"after sealing: {what} succeeded.",
                    fix="This is a bug in HyperBox, not your setup. Please "
                        "report it with the engine and version.",
                    context={"sandbox_id": handle.sandbox_id, "probe": what},
                )

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

        The reseal is also PROVEN, not assumed. create() has always probed
        from inside the container after sealing; this path re-opened the
        network mid-session and then trusted the detach to have worked --
        so the one code path that deliberately un-seals a live sandbox was
        the one path never checking the seal it put back. A leak here
        destroys the container rather than returning a sandbox that is
        described as sealed and is not.
        """
        failure = None
        code, out, err = 0, "", ""
        # The install budget, not the run() one -- same reasoning as
        # _provision. This path is deprecated but it is still a pip
        # install, and it was inheriting a ceiling sized for agent code.
        install_client = self._install_client(handle.backend)
        try:
            self._unseal(handle)
            code, out, err = api.run_exec(
                install_client, cid,
                ["python3", "-m", "pip", "install", "--no-input", *libraries],
            )
        except (TimeoutError, errors.ExecIncompleteError):
            failure = ExecResult(
                stdout="",
                stderr=(
                    f"Installing {', '.join(libraries)} did not finish within "
                    f"{policy.PROVISION_TIMEOUT_SECONDS:g}s. Declare packages "
                    "at create_sandbox(packages=[...]) instead, or ask the "
                    "user for an environment with them baked in."
                ),
                exit_code=-1,
            )
        except Exception as exc:  # noqa: BLE001
            failure = ExecResult(
                stdout="", stderr=f"Dependency install failed: {exc}", exit_code=-1
            )
        finally:
            self._seal(handle)

        # Deliberately outside the finally: raising from a finally would
        # swallow whatever was already in flight, and a leak must be the
        # error the caller sees, not a silent replacement for another one.
        try:
            self._assert_network_sealed(handle, client, cid)
        except errors.NetworkLeakError:
            try:
                api.remove_container(client, cid)
            except Exception:  # noqa: BLE001 - already failing
                pass
            raise

        if failure is not None:
            return failure
        if code != 0:
            return ExecResult(
                stdout=out, stderr=f"Dependency install failed:\n{err}",
                exit_code=code,
            )
        return None

    def start_background(self, handle: SandboxHandle, code: str) -> str:
        """Launch code that outlives the call, and return its process id.

        For a server the caller wants to test against: start it, then
        `run` a client in the same sandbox. Loopback works even though
        the sandbox is sealed -- detaching the networks leaves `lo`.

        The code goes in as a FILE and the shell only ever sees a quoted
        path, exactly as a foreground run does. Interpolating submitted
        code into a shell string is the one thing put_file exists to
        avoid: nothing is quoted, so nothing can be mis-quoted.

        The id is generated here rather than taken from the engine's exec
        id, because the log path has to be decided before the exec that
        writes to it is created.
        """
        spec = self._spec(handle.language)
        client = self._client(handle.backend)
        cid = self._ref(handle)

        run_id = uuid.uuid4().hex
        stem = spec.get("filename") or run_id
        path = f"{policy.CODE_DIR}/{stem}.{spec['extension']}"
        log = f"{policy.BACKGROUND_DIR}/{run_id}.log"
        api.put_file(client, cid, path, code.encode())

        argv = " ".join(shlex.quote(part) for part in [*spec["argv"], path])
        # ulimit -f counts 512-byte blocks and is a shell BUILTIN, so it
        # needs no binary the slim images might not ship -- the same rule
        # that keeps the timeout kill off `ps` and `pkill`.
        #
        # Piping through `head -c` was tried first and is wrong: head
        # buffers its output, so a daemon's log read back before ~4 KB had
        # accumulated came back EMPTY. "No logs" and "logs you cannot see
        # yet" are different answers, and returning the first for the
        # second is the failure this project is organised against.
        # Redirecting straight to the file shows every line immediately.
        blocks = policy.BACKGROUND_LOG_MAX_BYTES // 512
        # A later run's timeout kills its own process GROUP with
        # `kill -9 -<pid>`, and a background process must not be swept up
        # by it. It is not: each exec gets its own process tree, so the
        # daemon is already in a different group.
        #
        # setsid was tried here for that and removed. Mutation-tested: the
        # daemon survives a foreground timeout with and without it, so it
        # was not doing the job it was credited with -- and it is a
        # util-linux binary, which is the dependency the timeout kill
        # deliberately avoids by not using ps or pkill. nohup is a shell
        # builtin in the shells these images ship and covers SIGHUP.
        script = (
            f"mkdir -p {policy.BACKGROUND_DIR}; "
            f"nohup sh -c {shlex.quote(f'ulimit -f {blocks}; exec ' + argv)} "
            f"> {log} 2>&1 & "
            f"echo $! > {policy.BACKGROUND_DIR}/{run_id}.pid"
        )
        api.run_exec(client, cid, ["sh", "-c", script])
        return run_id

    def background_logs(self, handle: SandboxHandle, process_id: str) -> str:
        """Whatever the background run has written so far."""
        client = self._client(handle.backend)
        cid = self._ref(handle)
        log = f"{policy.BACKGROUND_DIR}/{process_id}.log"
        code, out, err = api.run_exec(
            client, cid, ["sh", "-c", f"cat {shlex.quote(log)} 2>/dev/null"]
        )
        if code != 0:
            raise sandbox_ops.SandboxRuntimeError(
                f"No background process '{process_id}' in this sandbox.",
                fix="Check the process_id returned by run(background=True). "
                    "Logs are lost when the sandbox is destroyed.",
                context={"process_id": process_id},
            )
        return out

    def _exec_with_deadline(self, handle, client, cid, code, timeout):
        """Run the code, and stop it for real if it outlives its timeout."""
        spec = self._spec(handle.language)
        run_id = uuid.uuid4().hex
        # Java's single-file mode requires the filename to match the public
        # class, so it gets a fixed name rather than a unique one. Every
        # run overwrites it, which is fine: runs are serialised per sandbox
        # by the registry lock.
        stem = spec.get("filename") or run_id
        path = f"{policy.CODE_DIR}/{stem}.{spec['extension']}"
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
            # A literal token, replaced -- not str.format. Most languages
            # use braces, so formatting their source as a template fails
            # on the code rather than on the marker.
            probe = spec["canary"].replace("MARKER", CANARY_MARKER)
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
