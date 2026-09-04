"""Phase 3: mount external MCP servers into HyperBox as live proxies.

This module owns ALL external-server wiring. `server.py` calls
`register(mcp)` through a find_spec seam and knows nothing else about
what happens here — that separation is what lets the mounting work and
the sandbox lifecycle proceed without touching each other's files.

Per DESIGN.md's "Adopt, don't build": we do not write an indexer or a
docs service, we mount existing ones.

- codebase indexing -> prefix `index`. DESIGN.md offers mcp-code-indexer
  (Qdrant-backed, for large repos) or semantic-search-mcp (pure local,
  for small ones). The default here is the local one: it needs no
  external service, which keeps `python -m hyperbox_mcp.server` runnable
  with nothing else set up.
- docs/best-practice -> prefix `docs`, Context7.

`mount()` (not `import_server()`) is deliberate: it creates a LIVE link,
forwarding each call to the backing server at request time. An indexer's
answers change as the files change, so a frozen one-time copy would be
wrong. See .claude/agents/mcp-composer.md.

Both mounts are lazy — no connection is made at import time, so a
backing server being down degrades that prefix's tools rather than
stopping HyperBox from starting. Set either env var below to "off"
to skip a mount entirely; an absent capability is simply absent, and is
never stubbed with a fake tool.
"""

from __future__ import annotations

import os
import shlex

from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

_OFF = {"off", "none", "disabled", ""}

# Context7's hosted endpoint. Override to point at a self-hosted one.
DOCS_URL = os.environ.get("HYPERBOX_MCP_DOCS_URL", "https://mcp.context7.com/mcp")

# The indexer is a stdio server. Override to swap in mcp-code-indexer,
# e.g. HYPERBOX_MCP_INDEX_CMD="mcp-code-indexer --qdrant-url http://...".
INDEX_CMD = os.environ.get("HYPERBOX_MCP_INDEX_CMD", "npx -y semantic-search-mcp")


def register(mcp: FastMCP) -> list[str]:
    """Mount the external servers onto `mcp`. Returns the prefixes
    actually mounted, so a caller (or a test) can see what is live
    rather than assuming both succeeded."""
    mounted: list[str] = []

    if INDEX_CMD.strip().lower() not in _OFF:
        parts = shlex.split(INDEX_CMD)
        index_proxy = FastMCP.as_proxy(
            StdioTransport(command=parts[0], args=parts[1:]),
            name="index",
        )
        mcp.mount(index_proxy, prefix="index")
        mounted.append("index")

    if DOCS_URL.strip().lower() not in _OFF:
        docs_proxy = FastMCP.as_proxy(
            StreamableHttpTransport(DOCS_URL),
            name="docs",
        )
        mcp.mount(docs_proxy, prefix="docs")
        mounted.append("docs")

    return mounted
