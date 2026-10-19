import asyncio
import base64
import sys
from unittest.mock import patch

import pytest
from flask import Flask
from mcp.types import CallToolResult, ImageContent, TextContent

from src.interfaces.chat_app import app as chat_app_module
from src.interfaces.chat_app.kb_figures import (
    FigureNotFound,
    fetch_kb_figure,
    image_from_tool_result,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"


def _image(data=PNG_BYTES, mime="image/png"):
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime)


def test_image_result_is_decoded_to_bytes_and_mime_type():
    result = CallToolResult(content=[_image(mime="image/jpeg")])

    assert image_from_tool_result(result) == (PNG_BYTES, "image/jpeg")


def test_error_result_raises_figure_not_found_with_server_message():
    result = CallToolResult(
        content=[TextContent(type="text", text="Image not found: _page_1_Figure_2.png")],
        isError=True,
    )

    with pytest.raises(FigureNotFound, match="Image not found"):
        image_from_tool_result(result)


def test_result_without_image_raises_figure_not_found():
    result = CallToolResult(content=[TextContent(type="text", text="no image here")])

    with pytest.raises(FigureNotFound):
        image_from_tool_result(result)


FIGURE_ID = "mu2e-docdb/55483-_page_11_Figure_8.png"

# A stand-in kb server serving one figure, run over stdio by the real MCP client.
FAKE_KB_SERVER = f'''
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("kb")

@mcp.tool()
def kb_get_image(identifier: str) -> Image:
    if identifier != {FIGURE_ID!r}:
        raise ValueError(f"Image not found: {{identifier}}")
    return Image(data={PNG_BYTES!r}, format="png")

mcp.run()
'''


def _fake_kb_server(tmp_path):
    script = tmp_path / "fake_kb.py"
    script.write_text(FAKE_KB_SERVER)
    # include_tools is the agent's allow-list; the route calls kb_get_image anyway.
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "include_tools": ["kb_search", "kb_get"],
    }


def test_fetch_kb_figure_returns_the_image_kb_serves(tmp_path):
    figure = asyncio.run(fetch_kb_figure(FIGURE_ID, _fake_kb_server(tmp_path)))

    assert figure == (PNG_BYTES, "image/png")


def test_fetch_kb_figure_raises_figure_not_found_for_an_unknown_id(tmp_path):
    with pytest.raises(FigureNotFound, match="Image not found"):
        asyncio.run(fetch_kb_figure("mu2e-docdb/no-such-figure.png", _fake_kb_server(tmp_path)))


# --- /api/kb-figure route -----------------------------------------------------

KB_SERVER = {"transport": "streamable_http", "url": "http://kb.example/mcp"}


def _get_figure(query, servers=None, fetched=(PNG_BYTES, "image/png")):
    wrapper = object.__new__(chat_app_module.FlaskAppWrapper)
    app = Flask(__name__)
    servers = {"kb": KB_SERVER} if servers is None else servers
    with app.test_request_context(f"/api/kb-figure?{query}"):
        with patch.object(chat_app_module, "get_mcp_servers_config", return_value=servers), \
             patch.object(chat_app_module, "get_kb_figure") as get_kb_figure:
            if isinstance(fetched, Exception):
                get_kb_figure.side_effect = fetched
            else:
                get_kb_figure.return_value = fetched
            response = app.make_response(wrapper.kb_figure())
    return response, get_kb_figure


def test_route_returns_the_figure_with_its_mime_type():
    response, get_kb_figure = _get_figure(f"id={FIGURE_ID}")

    assert response.status_code == 200
    assert response.data == PNG_BYTES
    assert response.mimetype == "image/png"
    assert "private" in response.headers["Cache-Control"]
    get_kb_figure.assert_called_once_with(FIGURE_ID, KB_SERVER)


def test_route_rejects_a_missing_id():
    response, get_kb_figure = _get_figure("id=")

    assert response.status_code == 400
    get_kb_figure.assert_not_called()


def test_route_returns_404_when_kb_server_is_not_configured():
    response, get_kb_figure = _get_figure(f"id={FIGURE_ID}", servers={})

    assert response.status_code == 404
    get_kb_figure.assert_not_called()


def test_route_returns_404_when_kb_has_no_such_figure():
    response, _ = _get_figure(f"id={FIGURE_ID}", fetched=FigureNotFound("Image not found"))

    assert response.status_code == 404


def test_route_returns_502_when_kb_cannot_be_reached():
    response, _ = _get_figure(f"id={FIGURE_ID}", fetched=ConnectionError("refused"))

    assert response.status_code == 502
