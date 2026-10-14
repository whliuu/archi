import asyncio

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.archi.pipelines.agents import base_react
from src.archi.pipelines.agents.utils.context_overflow import (
    ShrinkToolResultsOnOverflow,
    shrink_tool_results,
)

VLLM_OVERFLOW = (
    "Error code: 400 - {'error': {'message': \"Input length (139278) exceeds "
    "model's maximum context length (131072).\"}}"
)


def _turn(*results):
    messages = [HumanMessage(content="find the TS endcap design")]
    for i, text in enumerate(results):
        call_id = f"call_{i}"
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "kb__kb_search", "args": {}, "id": call_id}],
            )
        )
        messages.append(ToolMessage(content=text, tool_call_id=call_id))
    return messages


def _tool_texts(messages):
    return [m.content for m in messages if isinstance(m, ToolMessage)]


def test_shrink_halves_older_results_and_keeps_newest():
    messages = _turn("a" * 8000, "b" * 8000, "c" * 8000)
    shrunk = shrink_tool_results(messages, min_chars=1000)
    first, second, newest = _tool_texts(shrunk)
    assert first.startswith("a" * 4000) and "a" * 4001 not in first
    assert "first 4000 of 8000 characters" in first
    assert second.startswith("b" * 4000) and "b" * 4001 not in second
    assert newest == "c" * 8000
    assert shrunk[0] is messages[0]
    assert _tool_texts(messages) == ["a" * 8000, "b" * 8000, "c" * 8000]


def test_shrink_does_not_go_below_floor():
    shrunk = shrink_tool_results(_turn("a" * 1500, "b" * 100), min_chars=1000)
    assert _tool_texts(shrunk)[0].startswith("a" * 1000)
    assert "a" * 1001 not in _tool_texts(shrunk)[0]


def test_shrink_cuts_newest_once_older_results_are_at_floor():
    shrunk = shrink_tool_results(_turn("a" * 500, "c" * 8000), min_chars=1000)
    older, newest = _tool_texts(shrunk)
    assert older == "a" * 500
    assert newest.startswith("c" * 4000) and "c" * 4001 not in newest


def test_shrink_returns_none_when_nothing_can_be_cut():
    assert shrink_tool_results(_turn("a" * 500, "b" * 500), min_chars=1000) is None
    assert (
        shrink_tool_results([HumanMessage(content="x" * 10000)], min_chars=1000) is None
    )


def test_repeated_shrink_keeps_one_note_with_original_length():
    messages = _turn("a" * 16000, "z")
    for _ in range(2):
        messages = shrink_tool_results(messages, min_chars=1000)
    text = _tool_texts(messages)[0]
    assert text.startswith("a" * 4000) and "a" * 4001 not in text
    assert text.count("Shortened") == 1
    assert "first 4000 of 16000 characters" in text


def test_shrink_joins_multi_part_results():
    messages = _turn("z")
    messages.insert(
        2, ToolMessage(content=["a" * 3000, "b" * 3000], tool_call_id="call_x")
    )
    shrunk = shrink_tool_results(messages, min_chars=1000)
    text = _tool_texts(shrunk)[0]
    assert isinstance(text, str)
    assert "first 3000 of 6001 characters" in text


def _request(messages):
    return ModelRequest(
        model=None,
        system_prompt="system",
        messages=messages,
        tool_choice=None,
        tools=[],
        response_format=None,
        state={},
        runtime=None,
    )


def _middleware():
    return ShrinkToolResultsOnOverflow(
        base_react.BaseReActAgent._is_context_overflow_error
    )


def _overflow_until(max_chars, seen):
    def handler(request):
        seen.append(request)
        if sum(len(t) for t in _tool_texts(request.messages)) > max_chars:
            raise Exception(VLLM_OVERFLOW)
        return "answer"

    return handler


def test_middleware_retries_overflowing_call_with_shrunk_results():
    seen = []
    request = _request(_turn("a" * 40000, "b" * 40000, "c" * 40000))
    result = _middleware().wrap_model_call(request, _overflow_until(85000, seen))
    assert result == "answer"
    assert len(seen) == 2
    assert sum(len(t) for t in _tool_texts(seen[-1].messages)) <= 85000
    assert seen[-1].system_prompt == "system"
    assert _tool_texts(request.messages)[0] == "a" * 40000


def test_middleware_reraises_other_errors_without_retry():
    seen = []

    def handler(request):
        seen.append(request)
        raise ValueError("bad tool schema")

    with pytest.raises(ValueError, match="bad tool schema"):
        _middleware().wrap_model_call(_request(_turn("a" * 40000, "b")), handler)
    assert len(seen) == 1


def test_middleware_reraises_overflow_when_nothing_left_to_cut():
    seen = []
    with pytest.raises(Exception, match="maximum context length"):
        _middleware().wrap_model_call(
            _request(_turn("a" * 500)), _overflow_until(10, seen)
        )
    assert len(seen) == 1


def test_middleware_async_retries_overflowing_call():
    seen = []
    sync_handler = _overflow_until(85000, seen)

    async def handler(request):
        return sync_handler(request)

    request = _request(_turn("a" * 40000, "b" * 40000, "c" * 40000))
    result = asyncio.run(_middleware().awrap_model_call(request, handler))
    assert result == "answer"
    assert len(seen) == 2


def test_agent_adds_overflow_middleware_last(monkeypatch):
    observed = {}

    def fake_create_agent(**kwargs):
        observed.update(kwargs)
        return "agent"

    monkeypatch.setattr(base_react, "create_agent", fake_create_agent)
    agent = base_react.BaseReActAgent.__new__(base_react.BaseReActAgent)
    agent.agent_llm = "llm"
    monkeypatch.setattr(agent, "_build_system_prompt", lambda: "prompt")

    other = object()
    assert agent._create_agent([], [other]) == "agent"
    assert observed["middleware"][0] is other
    assert isinstance(observed["middleware"][-1], ShrinkToolResultsOnOverflow)
