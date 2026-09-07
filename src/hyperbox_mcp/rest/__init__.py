"""A Docker Engine REST client, built on the standard library.

Both engines implement the same HTTP API. Podman serves it deliberately —
its compatibility endpoint answers `/v1.44/version` and reports `bridge`
as a network name, exactly as Docker does — so one client reaches both and
the dialect differences that the SDKs forced on this project disappear.

Nothing here imports docker-py, podman-py or httpx. `http.client` reaches
the same API with no dependency at all, and no HTTP library would have
helped with the part that is actually hard: Windows named pipes, which
neither httpx nor httpcore can speak.
"""

from hyperbox_mcp.rest.transport import connection_for

__all__ = ["connection_for"]
