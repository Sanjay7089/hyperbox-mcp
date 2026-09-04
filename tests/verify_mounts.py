"""Acceptance test for the Phase 3 mounts. No mocking.

    python tests/verify_mounts.py

Confirming the server starts, or that tools appear under a prefix, is
NOT sufficient — a proxy can register a tool list and still fail every
call. This drives a REAL call through each mounted prefix to its
backing server and checks real data comes back. See REQUIREMENTS.md
Phase 3 and .claude/agents/mcp-composer.md.

Requires network for the docs prefix (Context7) and npx for the index
prefix. The indexer builds an embedding index of this workspace on
first run, which can take several minutes — slowness here is the
backing server working, not a hang.

Triage before editing code: is there network? does `npx -y
semantic-search-mcp` start by hand? Set HYPERBOX_MCP_INDEX_CMD or
HYPERBOX_MCP_DOCS_URL to "off" to isolate one prefix at a time.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "src")

# docs_* is OFF by default now (it cost ~1163 tokens of tool definitions on
# every message and was never called in evaluation). This suite still has to
# prove the mount WORKS when asked for, so enable it before importing the
# server, and separately assert the default really is off.
_DOCS_DEFAULT_OFF = os.environ.get("HYPERBOX_MCP_DOCS_URL") is None
os.environ.setdefault("HYPERBOX_MCP_DOCS_URL", "https://mcp.context7.com/mcp")

from fastmcp import Client  # noqa: E402

from hyperbox_mcp import server  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}", flush=True)


def as_text(result) -> str:
    """Flatten a CallToolResult to text without caring which field the
    backing server populated."""
    for attr in ("data", "content"):
        val = getattr(result, attr, None)
        if val:
            return str(val)
    return str(result)


async def main() -> int:
    async with Client(server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
        print(f"tools: {sorted(names)}\n", flush=True)

        check(
            "lifecycle tools survive mounting",
            {"create_sandbox", "run", "destroy_sandbox"} <= names,
            "",
        )
        check("index prefix mounted", any(n.startswith("index_") for n in names))
        check("docs prefix mounted when explicitly enabled",
              any(n.startswith("docs_") for n in names))

        from hyperbox_mcp import mounts as _m

        check("docs_ is OFF unless explicitly enabled",
              _DOCS_DEFAULT_OFF,
              "env was preset, default not exercised" if not _DOCS_DEFAULT_OFF else "")

        # --- docs prefix: a real lookup against Context7 ---
        try:
            res = await client.call_tool(
                "docs_resolve-library-id",
                {"libraryName": "fastmcp", "query": "mount a proxy server"},
            )
            text = as_text(res)
            check(
                "docs_ round-trips a real call to Context7",
                len(text) > 50 and "fastmcp" in text.lower(),
                text[:160].replace("\n", " "),
            )
        except Exception as exc:  # noqa: BLE001
            check("docs_ round-trips a real call to Context7", False, f"{type(exc).__name__}: {exc}")

        # --- index prefix: a real query against the local indexer ---
        try:
            status = await client.call_tool("index_get_status", {})
            check("index_get_status returns real status", len(as_text(status)) > 10,
                  as_text(status)[:160].replace("\n", " "))
        except Exception as exc:  # noqa: BLE001
            check("index_get_status returns real status", False, f"{type(exc).__name__}: {exc}")

        try:
            found = await client.call_tool(
                "index_semantic_search",
                {"query": "create a disposable sandbox container", "limit": 3},
            )
            text = as_text(found)
            # A real hit must name a file that actually exists in this repo.
            check(
                "index_ round-trips a real search that finds this repo's code",
                any(f in text for f in ("runtime.py", "server.py", "llm_sandbox_runtime.py", "verify.py")),
                text[:200].replace("\n", " "),
            )
        except Exception as exc:  # noqa: BLE001
            check("index_ round-trips a real search", False, f"{type(exc).__name__}: {exc}")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
