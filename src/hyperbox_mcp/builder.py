"""Building custom sandbox environments, from a terminal only.

This is deliberately not reachable from the MCP tool surface. A build
runs whatever the Dockerfile says — arbitrary commands, as root, with
network access, under none of the limits policy.py enforces on a
sandbox. That is a decision for the person at the keyboard, not for a
model. Agents consume the result by name; they never produce it.

Anything here may print to stdout: it runs under the `hyperbox` CLI,
never inside the server process, whose stdout belongs to the client.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hyperbox_mcp import engine, policy, validate

HYPERBOX_DIR = Path.home() / ".hyperbox"
ENV_DIR = HYPERBOX_DIR / "environments"


def list_environments() -> int:
    """Print every environment the server would currently resolve."""
    envs = policy.environments()
    print(f"{'ENVIRONMENT':<20} {'IMAGE':<48} {'DOCKERFILE'}")
    print("-" * 82)
    for name, image in sorted(envs.items()):
        dockerfile = ENV_DIR / name / "Dockerfile"
        print(f"{name:<20} {image:<48} {'yes' if dockerfile.exists() else 'built-in'}")
    return 0


def run_build(env_name: str, custom_path: str | None = None) -> int:
    """Build a local Dockerfile into the image `create_sandbox` will use."""
    # Validated here too, not only at the MCP boundary: env_name becomes a
    # directory under ~/.hyperbox and an image tag, and this path never
    # goes through the server.
    try:
        name = validate.environment_name(env_name)
    except validate.InvalidInput as exc:
        print(f"Error: {exc}")
        return 2

    env_dir = ENV_DIR / name
    dockerfile = env_dir / "Dockerfile"

    if custom_path:
        source = Path(custom_path).expanduser().resolve()
        if not source.is_file():
            print(f"Error: no Dockerfile at {source}")
            return 1
        env_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dockerfile)
        print(f"Copied {source} -> {dockerfile}")

    if not dockerfile.exists():
        print(f"Error: no Dockerfile at {dockerfile}")
        print("Provide one with:")
        print(f"  hyperbox build {name} --custom /path/to/Dockerfile")
        return 1

    try:
        backend = engine.detect("auto")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"Error: no container engine available: {exc}")
        return 1

    target_image = f"hyperbox-local/{name}:latest"
    binary = engine.client_dialect(backend)
    cmd = [binary, "build", "-t", target_image, "-f", str(dockerfile), str(env_dir)]

    print(f"Building '{name}' as {target_image} using {binary}...")
    try:
        result = subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\nBuild cancelled.")
        return 130
    except OSError as exc:
        print(f"\nCould not run {binary}: {exc}")
        return 1

    if result.returncode == 0:
        print(f"\nBuilt '{name}'. Use it with create_sandbox(environment='{name}').")
        print("A running server picks it up without a restart.")
    else:
        print(f"\nBuild failed with exit code {result.returncode}.")
    return result.returncode
