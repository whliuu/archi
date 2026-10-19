"""
Figures from the Mu2e knowledge base (the `kb` MCP server) for the chat UI.

kb stores each figure extracted from a DocDB talk or note as its own
document. The agent finds figures with kb_search and links them in its answer
as markdown images pointing at /api/kb-figure; that route fetches the image
through kb's kb_get_image tool, so kb's token stays on the server and kb's
visibility and redaction rules still apply. The LLM never sees the pixels.
"""
import base64
from typing import Any, Dict, Tuple

from mcp.types import CallToolResult, ImageContent, TextContent

from src.archi.pipelines.agents.tools.mcp import build_client_config
from src.archi.pipelines.agents.utils.mcp_utils import AsyncLoopThread

KB_SERVER_NAME = "kb"
KB_IMAGE_TOOL = "kb_get_image"
# One figure is tens of kB; a fetch that takes longer than this has stalled.
KB_FIGURE_TIMEOUT_SECONDS = 30.0


class FigureNotFound(Exception):
    """kb has no figure under the requested id, or withholds it."""


def image_from_tool_result(result: CallToolResult) -> Tuple[bytes, str]:
    """Return (image bytes, MIME type) from a kb_get_image result."""
    if result.isError:
        message = " ".join(c.text for c in result.content if isinstance(c, TextContent))
        raise FigureNotFound(message or f"{KB_IMAGE_TOOL} failed")
    for content in result.content:
        if isinstance(content, ImageContent):
            return base64.b64decode(content.data), content.mimeType
    raise FigureNotFound(f"{KB_IMAGE_TOOL} returned no image")


async def fetch_kb_figure(identifier: str, server_cfg: Dict[str, Any]) -> Tuple[bytes, str]:
    """
    Fetch one figure from the kb server. The tool is called directly, so the
    server's `include_tools` (the agent's allow-list) does not apply.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    cfg, missing = build_client_config(server_cfg)
    if missing:
        raise RuntimeError(f"kb secret(s) not set: {missing}")
    client = MultiServerMCPClient({KB_SERVER_NAME: cfg})
    async with client.session(KB_SERVER_NAME) as session:
        result = await session.call_tool(KB_IMAGE_TOOL, {"identifier": identifier})
    return image_from_tool_result(result)


def get_kb_figure(identifier: str, server_cfg: Dict[str, Any]) -> Tuple[bytes, str]:
    """Blocking fetch_kb_figure for Flask handlers, run on archi's MCP event loop."""
    return AsyncLoopThread.get_instance().run(
        fetch_kb_figure(identifier, server_cfg), timeout=KB_FIGURE_TIMEOUT_SECONDS
    )
