import asyncio
from types import SimpleNamespace

from src.archi.pipelines.agents.utils.mcp_utils import (
    cap_tool_output,
    truncate_tool_text,
)


def test_text_under_limit_is_unchanged():
    assert truncate_tool_text("short result", 100) == "short result"


def test_text_over_limit_is_cut_with_note():
    out = truncate_tool_text("a" * 50, 10)
    assert out.startswith("a" * 10)
    assert "a" * 11 not in out
    assert "first 10 of 50 characters" in out


def test_multi_part_result_over_limit_is_joined_and_cut():
    out = truncate_tool_text(["a" * 8, "b" * 8], 10)
    assert isinstance(out, str)
    assert out.startswith("a" * 8 + "\n" + "b")
    assert "first 10 of 17 characters" in out


def test_multi_part_result_under_limit_is_unchanged():
    assert truncate_tool_text(["one", "two"], 100) == ["one", "two"]


def test_non_positive_limit_disables_cap():
    assert truncate_tool_text("a" * 50, 0) == "a" * 50


def _tool(content, artifact=None):
    async def call_tool(**arguments):
        return content, artifact

    return SimpleNamespace(name="kb_search", coroutine=call_tool)


def test_cap_tool_output_cuts_result_and_keeps_artifact():
    artifact = [object()]
    tool = _tool("a" * 50, artifact)
    cap_tool_output(tool, 10)
    content, returned_artifact = asyncio.run(tool.coroutine(query="x"))
    assert content.startswith("a" * 10)
    assert "first 10 of 50 characters" in content
    assert returned_artifact is artifact


def test_cap_tool_output_passes_arguments_through():
    seen = {}

    async def call_tool(**arguments):
        seen.update(arguments)
        return "ok", None

    tool = SimpleNamespace(name="kb_search", coroutine=call_tool)
    cap_tool_output(tool, 10)
    asyncio.run(tool.coroutine(query="x", limit=3))
    assert seen == {"query": "x", "limit": 3}


def test_cap_tool_output_skips_tools_without_coroutine():
    tool = SimpleNamespace(name="kb_search")
    cap_tool_output(tool, 10)
    assert not hasattr(tool, "coroutine")
