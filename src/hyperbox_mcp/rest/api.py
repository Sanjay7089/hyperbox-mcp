"""Typed calls onto the engine's REST API.

Thin on purpose: each function is one request, named for what it does, so
the call sites read as intent rather than as URL construction. Anything
that needs judgement — what to do when a limit was not applied, whether a
container is ours — belongs in sandbox_ops, not here.

The dialect problem is gone. Speaking REST directly, both engines take one
Docker-shaped HostConfig, which deletes the translation layer the SDKs
forced: podman-py silently discarded `nano_cpus` and rejected `tmpfs`
outright, so the same policy had to be written twice and the second
spelling was the one nobody checked. Podman's compatibility endpoint
accepts the Docker spelling of all of it.

One conditional survives, and it is not a dialect: Docker rejects NanoCpus
and CpuQuota together, so the CPU ceiling is written the way the engine
that is actually answering expects. Which engine that is comes from
identify(), never from which socket answered — Podman commonly serves
Docker's endpoint.
"""

from __future__ import annotations

import io
import json
from urllib.parse import quote
import tarfile
from typing import Any, Iterator

from hyperbox_mcp import errors
from hyperbox_mcp.policy import (
    CODE_DIR,
    CPU_PERIOD,
    CPU_QUOTA,
    LABEL_ID,
    LABEL_MANAGED,
    MEM_LIMIT_BYTES,
    MEM_SWAP_BYTES,
    NANO_CPUS,
    NO_NEW_PRIVILEGES,
    PIDS_LIMIT,
    TMPFS_PATHS,
    TMPFS_SIZE,
)
from hyperbox_mcp.rest.client import EngineClient, _message


def identify(client: EngineClient) -> str:
    """Which engine is actually answering — the product, not the pipe.

    Delegates to engine.identify_version so there is exactly one
    implementation. Two of them disagreed on Windows once, which is why
    this is a one-line wrapper rather than a copy.
    """
    from hyperbox_mcp import engine

    return engine.identify_version(client.version)


def host_config(product: str) -> dict:
    """The resource ceiling, in the one shape both engines accept.

    Read back off the created container afterwards, always. An engine that
    accepts a limit and applies nothing hands back a container this server
    would go on describing as limited, which is the worst outcome available
    here.
    """
    config: dict[str, Any] = {
        "Memory": MEM_LIMIT_BYTES,
        # Without this Docker defaults it to twice Memory. See policy.
        "MemorySwap": MEM_SWAP_BYTES,
        "PidsLimit": PIDS_LIMIT,
        "Tmpfs": {path: f"rw,size={TMPFS_SIZE},mode=1777" for path in TMPFS_PATHS},
        "SecurityOpt": ["no-new-privileges"] if NO_NEW_PRIVILEGES else [],
    }
    if product == "podman":
        # Podman's compat endpoint honours the quota pair. Docker refuses
        # to accept it alongside NanoCpus, so only one spelling is sent.
        config["CpuQuota"] = CPU_QUOTA
        config["CpuPeriod"] = CPU_PERIOD
    else:
        config["NanoCpus"] = NANO_CPUS
    return config


# --- containers -------------------------------------------------------


def create_container(
    client: EngineClient, image: str, sandbox_id: str, command: list[str]
) -> str:
    """Create a labelled, limited container and return its id.

    The command is explicit rather than inherited from the image. Relying
    on an image's own CMD to keep a container alive makes the sandbox's
    lifetime a property of whichever image was chosen, which breaks the
    moment someone selects a different one.
    """
    body = {
        "Image": image,
        "Cmd": command,
        "Labels": {LABEL_MANAGED: "true", LABEL_ID: sandbox_id},
        "Env": ["PYTHONUNBUFFERED=1"],
        "WorkingDir": "/work",
        "HostConfig": host_config(identify(client)),
    }
    created = client.request("POST", "/containers/create", body)
    container_id = (created or {}).get("Id")
    if not container_id:
        raise errors.ProvisionError(
            "The engine created a container but returned no id.",
            context={"image": image, "sandbox_id": sandbox_id},
        )
    return container_id


def start_container(client: EngineClient, cid: str) -> None:
    client.request("POST", f"/containers/{cid}/start", expect=(204, 304))


def inspect_container(client: EngineClient, cid: str) -> dict:
    return client.request("GET", f"/containers/{cid}/json")


def remove_container(client: EngineClient, cid: str, force: bool = True) -> None:
    suffix = "?force=true&v=true" if force else "?v=true"
    client.request("DELETE", f"/containers/{cid}{suffix}", expect=(200, 204))


def restart_container(client: EngineClient, cid: str, timeout: int = 2) -> None:
    client.request(
        "POST", f"/containers/{cid}/restart?t={timeout}", expect=(204, 200)
    )


def list_containers(
    client: EngineClient, all: bool = False, filters: dict | None = None
) -> list[dict]:
    """Containers, optionally filtered. Filters are percent-encoded."""
    query = f"all={'true' if all else 'false'}"
    if filters:
        # The API wants every filter value as a LIST; the SDK accepted a
        # bare string. Normalising here means callers written against the
        # old shape keep working instead of silently filtering nothing.
        shaped = {
            key: value if isinstance(value, list) else [value]
            for key, value in filters.items()
        }
        encoded = json.dumps(shaped, separators=(",", ":"))
        query += f"&filters={quote(encoded)}"
    return client.request("GET", f"/containers/json?{query}") or []


def list_managed(client: EngineClient) -> list[dict]:
    """Every container carrying our label, running or not.

    The filter is a JSON document in a query string, so it has to be
    percent-encoded: unencoded, the space in `{"label": [...]}` makes
    http.client reject the URL outright as containing control
    characters. Compact separators keep it short as well as legal.
    """
    filters = json.dumps(
        {"label": [f"{LABEL_MANAGED}=true"]}, separators=(",", ":")
    )
    return client.request(
        "GET", f"/containers/json?all=true&filters={quote(filters)}"
    ) or []


# --- exec -------------------------------------------------------------


def exec_create(
    client: EngineClient, cid: str, argv: list[str], user: str | None = None
) -> str:
    """Create an exec. `user` overrides the image's own USER for this one.

    Omitted means "whatever the image says", which is what agent code
    must always get: an image hardened to run as a non-root user keeps
    that. It is passed only for the handful of setup steps that have to
    write outside that user's reach, and those are server-authored argv,
    never anything a caller supplied.
    """
    body: dict[str, Any] = {
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,  # framing depends on this; see client.stream_frames
        "Cmd": argv,
    }
    if user is not None:
        body["User"] = user
    return client.request("POST", f"/containers/{cid}/exec", body)["Id"]


def exec_running(client: EngineClient, exec_id: str) -> bool:
    try:
        return bool(client.request("GET", f"/exec/{exec_id}/json").get("Running"))
    except Exception:  # noqa: BLE001 - unknown means "stop waiting"
        return False


def exec_start(client: EngineClient, exec_id: str) -> tuple[str, str]:
    """Run it and return demultiplexed (stdout, stderr).

    The completion check is passed in because some transports never see the
    connection close — see EngineClient._read_until_done.
    """
    return client.stream_frames(
        f"/exec/{exec_id}/start", {"Detach": False, "Tty": False},
        is_finished=lambda: not exec_running(client, exec_id),
    )


def exec_exit_code(client: EngineClient, exec_id: str) -> int:
    """The real exit code, asked for separately.

    Not optional: the streamed start call does not carry it, and both SDKs
    return None for it when streaming — a trap this path avoids by always
    asking.

    A RUNNING exec has no exit code, and refusing to invent one is the
    whole point of the check below. This used to read `.get("ExitCode")
    or 0`, so an exec the reader had given up on -- Docker reports
    `ExitCode: null` while it is still going -- came back as a clean
    success. `_provision` then passed its `if code != 0` check and
    `create()` sealed the sandbox around a half-finished install. Verified
    against a real engine: a running exec reports
    `Running: True, ExitCode: None`, and `None or 0` is 0.
    """
    state = client.request("GET", f"/exec/{exec_id}/json")
    code = state.get("ExitCode")
    running = bool(state.get("Running"))
    # `Running` FIRST, and the engines differ on exactly this. Measured,
    # both on a `sleep 5` queried one second in:
    #
    #   docker  while running -> Running=True  ExitCode=None
    #   podman  while running -> Running=True  ExitCode=0
    #
    # So on Podman a running exec is byte-for-byte indistinguishable from
    # a successful one if you look only at the exit code -- there is no
    # null to notice. Keying on ExitCode alone fixes Docker and leaves
    # Podman reporting a half-finished install as a clean success.
    if running or code is None:
        raise errors.ExecIncompleteError(
            "The engine has no exit code for this command: it had not "
            f"finished{' and is still running' if running else ''}.",
            fix="It outlived the time HyperBox waited for it. For a heavy "
                "dependency install, bake it into an environment instead: "
                "hyperbox build <name> --dockerfile <path>.",
            context={"exec_id": exec_id, "running": running},
        )
    return int(code)


def run_exec(
    client: EngineClient, cid: str, argv: list[str], user: str | None = None
) -> tuple[int, str, str]:
    """Create, run and collect one exec: (exit_code, stdout, stderr)."""
    exec_id = exec_create(client, cid, argv, user=user)
    out, err = exec_start(client, exec_id)
    return exec_exit_code(client, exec_id), out, err


# --- images -----------------------------------------------------------


def _check_upload(status: int, raw: bytes, dest: str, what: str) -> None:
    """Fail loudly on a rejected archive upload.

    Both upload paths used to discard the status entirely. A 4xx/5xx from
    the archive endpoint then surfaced much later and somewhere else --
    as a file that was not there -- which reads as a bug in whatever went
    looking for it rather than in the write. `get_archive` has always
    checked; these now do too.
    """
    if status not in (200, 204):
        raise errors.ProvisionError(
            f"The engine refused to write {what} into {dest} "
            f"({status}): {_message(raw)}",
            fix="Check the destination exists in the sandbox and is not "
                "under a read-only or tmpfs mount.",
            context={"dest": dest, "status": status},
        )


def put_tree(client: EngineClient, cid: str, dest: str, tar_bytes: bytes) -> None:
    """Extract a prepared tar into `dest` inside the container.

    Same endpoint as put_file and the same tmpfs trap: on Docker a write
    under a tmpfs mount returns 200 and lands in the image layer beneath
    it, where nothing can see it. Callers sync into CODE_DIR for that
    reason, and this refuses anything else rather than no-op.
    """
    for mount in TMPFS_PATHS:
        if dest == mount or dest.startswith(mount + "/"):
            raise errors.ProvisionError(
                f"Cannot extract into {dest}: {mount} is a tmpfs mount, "
                "and on Docker the write lands in the layer underneath it "
                "and is never visible.",
                fix=f"Sync into {CODE_DIR} instead.",
                context={"dest": dest, "tmpfs": mount},
            )
    status, raw = client._raw(  # noqa: SLF001 - a tar body is not JSON
        "PUT",
        f"{client.api}/containers/{cid}/archive?path={dest}",
        tar_bytes,
        {"Content-Type": "application/x-tar"},
    )
    _check_upload(status, raw, dest, "a directory tree")


def get_archive(client: EngineClient, cid: str, path: str) -> bytes:
    """The tar the engine produces for `path` inside the container.

    Returned whole rather than streamed: the caller caps what it will
    accept before extracting, and a bounded read is simpler to reason
    about than a bounded stream. Everything in here was written by code
    running in the sandbox, so the extraction on the other side treats it
    as hostile.
    """
    status, raw = client._raw(  # noqa: SLF001 - a tar body is not JSON
        "GET", f"{client.api}/containers/{cid}/archive?path={path}"
    )
    if status == 404:
        raise errors.ProvisionError(
            f"{path} does not exist in the sandbox.",
            fix="Check the path. `run` a listing first if you are unsure.",
            context={"path": path},
        )
    if status != 200:
        raise errors.EngineRefusedError(
            f"GET archive {path} returned {status}: {_message(raw)}",
            context={"path": path, "status": status},
        )
    return raw


def image_present(client: EngineClient, image: str) -> bool:
    """Whether the engine already has this image.

    A 404 is the answer to the question, not a failure. Anything else the
    engine refused -- a bad reference, a permissions problem -- is a real
    error and must stay loud rather than be reported as "absent", which
    would send the caller into a pull that fails for the same reason.
    """
    try:
        client.request("GET", f"/images/{image}/json")
        return True
    except errors.EngineRefusedError as exc:
        if exc.context.get("status") == 404:
            return False
        raise


def pull_image(client: EngineClient, image: str) -> Iterator[dict]:
    """Pull, yielding the engine's own progress events as they arrive.

    Richer than either SDK exposes, and the reason `hyperbox build` and
    `doctor --pull` can show real layer progress rather than a spinner.
    """
    reference = image if ":" in image.rsplit("/", 1)[-1] else f"{image}:latest"
    yield from client.stream_json(
        "POST", f"/images/create?fromImage={reference}"
    )


def tag_image(client: EngineClient, image: str, repo: str, tag: str) -> None:
    client.request(
        "POST", f"/images/{image}/tag?repo={repo}&tag={tag}", expect=(201, 200)
    )


def build_image(
    client: EngineClient, context: bytes, tag: str, dockerfile: str = "Dockerfile",
    no_cache: bool = False,
) -> Iterator[dict]:
    """Build from a tar'd context, yielding the engine's progress events.

    The context is a tar the CALLER assembles, because the build API takes
    nothing else — and because assembling it is where .dockerignore has to
    be honoured. The engine does not read that file; the client is expected
    to have excluded already.
    """
    query = f"?t={tag}&dockerfile={dockerfile}"
    if no_cache:
        query += "&nocache=true"
    yield from client.stream_json(
        "POST", f"/build{query}", body=context,
        headers={"Content-Type": "application/x-tar"},
    )


# --- networks ---------------------------------------------------------


def list_networks(client: EngineClient) -> list[dict]:
    return client.request("GET", "/networks") or []


def disconnect_network(client: EngineClient, name: str, cid: str) -> None:
    client.request(
        "POST", f"/networks/{name}/disconnect",
        {"Container": cid, "Force": True}, expect=(200, 204),
    )


def connect_network(client: EngineClient, name: str, cid: str) -> None:
    client.request(
        "POST", f"/networks/{name}/connect", {"Container": cid},
        expect=(200, 204),
    )


# --- files ------------------------------------------------------------


def put_file(client: EngineClient, cid: str, path: str, content: bytes) -> None:
    """Write one file into the container.

    Through a tar archive, which is the only thing the API accepts — and
    the reason submitted code never passes through a shell on its way in.
    Nothing is quoted, so nothing can be mis-quoted.
    """
    directory, _, name = path.rpartition("/")
    # Refuse rather than no-op. Writing under a tmpfs mount succeeds with a
    # 200 on Docker and puts the file in the image layer beneath the mount,
    # where nothing can see it; Podman writes through, so the same call
    # works on one engine and silently vanishes on the other. Measured on
    # both. Submitted code goes to policy.CODE_DIR for this reason.
    for mount in TMPFS_PATHS:
        if directory == mount or directory.startswith(mount + "/"):
            raise errors.ProvisionError(
                f"Cannot write {path} through the engine's archive API: "
                f"{mount} is a tmpfs mount, and on Docker the write lands "
                "in the layer underneath it and is never visible.",
                fix=f"Write files under {CODE_DIR} instead; code running "
                    f"inside the sandbox can still use {mount} normally.",
                context={"path": path, "tmpfs": mount},
            )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(content))
    status, raw = client._raw(  # noqa: SLF001 - a tar body is not JSON
        "PUT",
        f"{client.api}/containers/{cid}/archive?path={directory or '/'}",
        buffer.getvalue(),
        {"Content-Type": "application/x-tar"},
    )
    _check_upload(status, raw, directory or "/", name)
