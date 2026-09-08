"""Building and registering sandbox environments, from a terminal only.

Deliberately not on the MCP tool surface. A build runs whatever the
Dockerfile says — arbitrary commands, as root, with network access, under
none of the limits policy.py enforces on a sandbox. That is a decision for
the person at the keyboard, not for a model. Agents consume the result by
name; they never produce it.

Two ways in, because environments arrive two ways:

    --dockerfile <path>   build one, from a file or a directory
    --image <ref>         pull one that already exists

Everything goes through the REST driver. The previous version shelled out
to a binary whose name came from `client_dialect()`, which meant it ran
`docker build` for a Podman backend on Windows and failed outright on macOS
where Podman lives off PATH — while its API was answering perfectly well.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from hyperbox_mcp import engine, errors, policy, validate
from hyperbox_mcp.buildcontext import build_tar
from hyperbox_mcp.progress import EngineProgress, say
from hyperbox_mcp.rest import api
from hyperbox_mcp.rest.client import EngineClient

def env_dir() -> Path:
    """Where to write environments. Follows policy, including its
    HYPERBOX_ENV_DIR override, rather than keeping its own copy."""
    return policy.env_dir()


def _resolve_engine(choice: str) -> tuple[EngineClient, engine.Resolution]:
    """Pick an engine and say which, before anything slow happens."""
    say("Checking container engines...", icon="search")
    resolution = engine.resolve(choice)
    print(resolution.banner())
    print()
    return EngineClient(resolution.endpoint, timeout=1800.0), resolution


def _write_manifest(name: str, image: str, source: str, product: str) -> Path:
    """Record what this environment is, so the server can resolve it.

    A pulled image has no Dockerfile and no predictable tag, so the
    directory alone can no longer say what to run. The manifest can.
    """
    directory = env_dir() / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "env.json"
    manifest.write_text(
        json.dumps(
            {
                "image": image,
                "source": source,
                "engine": product,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def list_environments() -> int:
    """Print every environment the server would currently resolve."""
    envs = policy.environments()
    print(f"{'ENVIRONMENT':<20} {'IMAGE':<46} SOURCE")
    print("-" * 84)
    for name, image in sorted(envs.items()):
        directory = env_dir() / name
        manifest = directory / "env.json"
        if manifest.is_file():
            try:
                source = json.loads(manifest.read_text()).get("source", "built")
            except (OSError, json.JSONDecodeError):
                source = "manifest unreadable"
        elif (directory / "Dockerfile").is_file():
            source = "dockerfile"
        else:
            source = "built-in"
        print(f"{name:<20} {image:<46} {source}")
    return 0


def run_build(
    env_name: str,
    dockerfile: str | None = None,
    image: str | None = None,
    engine_choice: str = "auto",
    no_cache: bool = False,
) -> int:
    try:
        name = validate.environment_name(env_name)
    except errors.InvalidInput as exc:
        say(str(exc), icon="fail")
        return 2

    if dockerfile and image:
        say("Use --dockerfile or --image, not both.", icon="fail")
        return 2
    if not dockerfile and not image:
        existing = env_dir() / name / "Dockerfile"
        if not existing.is_file():
            say(f"Nothing to build '{name}' from.", icon="fail")
            print("  hyperbox build <name> --dockerfile <path>   build one")
            print("  hyperbox build <name> --image <ref>         pull one")
            return 2
        dockerfile = str(existing)

    try:
        client, resolution = _resolve_engine(engine_choice)
    except errors.NoEngineError as exc:
        say(exc.message, icon="fail")
        print()
        print(exc.fix)
        return 1
    except errors.HyperBoxError as exc:
        say(str(exc), icon="fail")
        return 1

    target = f"hyperbox-local/{name}"
    try:
        if image:
            return _pull(client, resolution, image, target, name)
        return _build(client, resolution, Path(dockerfile), target, name, no_cache)
    except errors.HyperBoxError as exc:
        say(exc.message, icon="fail")
        if exc.fix:
            print(f"  {exc.fix}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


def _pull(client, resolution, reference, target, name) -> int:
    say(f"Pulling {reference}...", icon="pull")
    with EngineProgress(f"pulling {reference}") as progress:
        for event in api.pull_image(client, reference):
            progress.update(event)
    api.tag_image(client, reference, target, "latest")
    manifest = _write_manifest(name, f"{target}:latest", "image", resolution.product)
    _done(name, f"{target}:latest", manifest)
    return 0


def _build(client, resolution, path, target, name, no_cache) -> int:
    """Build from a Dockerfile, which may be a file or a directory."""
    path = path.expanduser().resolve()
    if path.is_dir():
        context, dockerfile = path, path / "Dockerfile"
        if not dockerfile.is_file():
            say(f"No Dockerfile in {context}.", icon="fail")
            return 1
    elif path.is_file():
        # A bare Dockerfile: its own directory is the context, which is
        # what a person means by `--dockerfile ./Dockerfile`.
        context, dockerfile = path.parent, path
    else:
        say(f"No such file or directory: {path}", icon="fail")
        return 1

    say(f"Packing context from {context}", icon="pack")
    blob, inner_name, count = build_tar(context, dockerfile)
    print(f"  {count} file(s), {len(blob) / 1024:.0f} KB "
          f"(.dockerignore applied)")

    say(f"Building '{name}' as {target}:latest with {resolution.product}...",
        icon="build")
    failed = None
    with EngineProgress(f"building {name}") as progress:
        for event in api.build_image(
            client, blob, f"{target}:latest", inner_name, no_cache
        ):
            progress.update(event)
            if "error" in event:
                failed = event.get("error", "").strip()
    if failed:
        say(f"Build failed: {failed[:300]}", icon="fail")
        return 1

    manifest = _write_manifest(name, f"{target}:latest", "dockerfile",
                               resolution.product)
    _done(name, f"{target}:latest", manifest)
    return 0


def _done(name: str, image: str, manifest: Path) -> None:
    say(f"Built '{name}'", icon="ok")
    print(f"  image    {image}")
    print(f"  manifest {manifest}")
    print(f"  use it   create_sandbox(environment='{name}')")
    print("  A running server picks it up without a restart.")
