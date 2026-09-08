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
    NANO_CPUS,
    NO_NEW_PRIVILEGES,
    PIDS_LIMIT,
    TMPFS_PATHS,
    TMPFS_SIZE,
)
from hyperbox_mcp.rest.client import EngineClient


def identify(client: EngineClient) -> str:
    """Which engine is actually answering — the product, not the pipe.

    Podman serves a Docker-compatible endpoint, so "something answered"
    says nothing about what is running. The API distinguishes them plainly:

        docker  Components: ['Engine', 'containerd', 'runc', ...]
        podman  Components: ['Podman Engine', 'Conmon', 'OCI Runtime']
    """
    raw = client.version
    names = " ".join(
        str(c.get("Name", "")) for c in (raw.get("Components") or [])
    ).lower()
    blob = f"{names} {raw.get('Version', '')} {raw.get('Platform', '')}".lower()
    return "podman" if "podman" in blob else "docker"


def host_config(product: str) -> dict:
    """The resource ceiling, in the one shape both engines accept.

    Read back off the created container afterwards, always. An engine that
    accepts a limit and applies nothing hands back a container this server
    would go on describing as limited, which is the worst outcome available
    here.
    """
    config: dict[str, Any] = {
        "Memory": MEM_LIMIT_BYTES,
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


def list_managed(client: EngineClient) -> list[dict]:
    """Every container carrying our label, running or not."""
    filters = json.dumps({"label": [f"{LABEL_MANAGED}=true"]})
    return client.request("GET", f"/containers/json?all=true&filters={filters}") or []


# --- exec -------------------------------------------------------------


def exec_create(client: EngineClient, cid: str, argv: list[str]) -> str:
    body = {
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,  # framing depends on this; see client.stream_frames
        "Cmd": argv,
    }
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
    """
    return client.request("GET", f"/exec/{exec_id}/json").get("ExitCode") or 0


def run_exec(client: EngineClient, cid: str, argv: list[str]) -> tuple[int, str, str]:
    """Create, run and collect one exec: (exit_code, stdout, stderr)."""
    exec_id = exec_create(client, cid, argv)
    out, err = exec_start(client, exec_id)
    return exec_exit_code(client, exec_id), out, err


# --- images -----------------------------------------------------------


def image_present(client: EngineClient, image: str) -> bool:
    try:
        client.request("GET", f"/images/{image}/json")
        return True
    except errors.ContainerGoneError:
        return False


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
    client._raw(  # noqa: SLF001 - a tar body is not JSON
        "PUT",
        f"{client.api}/containers/{cid}/archive?path={directory or '/'}",
        buffer.getvalue(),
        {"Content-Type": "application/x-tar"},
    )
