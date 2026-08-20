"""Model Context Protocol (MCP) helpers.

Two directions, both optional and gracefully degrading:
  * `mcp_toolset(...)` lets an agent *consume* its tools from a stdio MCP server
    (returns an ADK `McpToolset`, or None if MCP isn't installed / is disabled),
    so tools can be served out-of-process without the core hard-depending on it.
  * `serve_stdio(tools, name)` *serves* a list of plain functions as an MCP
    stdio server (used by an app's `python -m app.mcp_server` entry point).

Install the extra: `pip install 'agent-core[mcp]'`.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional, Sequence


def _enabled(env_flag: str) -> bool:
    return os.getenv(env_flag, "").strip().lower() in {"1", "true", "yes", "on"}


def mcp_toolset(
    server_module: str,
    *,
    tool_filter: Optional[Sequence[str]] = None,
    env_flag: str = "AGENT_MCP_TOOLS",
):
    """Return an ADK McpToolset backed by a stdio MCP server, or None.

    Launches `python -m <server_module>` as a stdio MCP server and exposes its
    tools to the agent. Enabled via `env_flag`. Degrades to None (so the agent
    falls back to in-process function tools) if `mcp` isn't installed or anything
    goes wrong — the working core never hard-depends on it.
    """
    if not _enabled(env_flag):
        return None
    try:
        from google.adk.tools.mcp_tool import McpToolset
        from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
        from mcp import StdioServerParameters

        return McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(command=sys.executable, args=["-m", server_module])
            ),
            tool_filter=list(tool_filter) if tool_filter else None,
        )
    except Exception as exc:  # pragma: no cover - optional path
        print(f"[agent-core] MCP toolset unavailable ({exc}); using in-process tools.")
        return None


def serve_stdio(tools: Sequence[Callable], name: str = "agent-core-tools") -> None:
    """Serve `tools` (plain functions) as an MCP stdio server. Blocks.

    Minimal wrapper: converts each function to an MCP tool via ADK's adapter and
    runs a stdio server. Call from an app's `if __name__ == "__main__":` guard.
    """
    import asyncio

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent

    try:
        from google.adk.tools.function_tool import FunctionTool
    except Exception:  # pragma: no cover
        FunctionTool = None  # type: ignore

    server = Server(name)
    wrapped = {t.__name__: (FunctionTool(t) if FunctionTool else None, t) for t in tools}

    @server.list_tools()
    async def _list():  # noqa: ANN202
        from mcp.types import Tool

        out = []
        for fname, (ft, raw) in wrapped.items():
            schema = {}
            if ft is not None:
                try:
                    decl = ft._get_declaration()  # ADK builds the JSON schema
                    schema = (decl.parameters.model_dump() if decl and decl.parameters else {}) or {}
                except Exception:
                    schema = {"type": "object", "properties": {}}
            out.append(Tool(name=fname, description=(raw.__doc__ or fname).strip(),
                            inputSchema=schema or {"type": "object", "properties": {}}))
        return out

    @server.call_tool()
    async def _call(tool_name: str, arguments: dict):  # noqa: ANN202
        entry = wrapped.get(tool_name)
        if entry is None:
            return [TextContent(type="text", text=f"unknown tool {tool_name!r}")]
        _, raw = entry
        result = raw(**(arguments or {}))
        return [TextContent(type="text", text=str(result))]

    async def _run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())
