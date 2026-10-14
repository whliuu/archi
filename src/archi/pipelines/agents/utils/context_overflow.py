"""
Recover from a context-window overflow partway through an agent turn.

Tool results stay in the agent's messages for the rest of a turn, so a turn
with many tool calls can outgrow the model's context window even when each
result is capped. When a model call overflows, the middleware here shortens
older tool results and retries that call instead of failing the whole turn.
Shortening is applied to the request only; the agent's state keeps the full
results.
"""

import re
from typing import Any, Callable, List, Optional, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, ToolMessage

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Shortened results keep at least this many characters (the top of a ranked
# search result list is usually the useful part).
MIN_TOOL_RESULT_CHARS = 2000
# Each round halves the results, so 5 rounds cut a 40k-character result to 2k.
MAX_SHRINK_ROUNDS = 5

_NOTE = "\n\n[Shortened to fit the model's context window: first {kept} of {total} characters.]"
_NOTE_RE = re.compile(
    r"\n\n\[Shortened to fit the model's context window: first \d+ of (\d+) characters\.\]$"
)


def _tool_text(content: Any) -> Optional[str]:
    """A tool result's text: a str, or a list of str joined; None for other content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(part, str) for part in content):
        return "\n".join(content)
    return None


def _shorten(message: ToolMessage, min_chars: int) -> Optional[ToolMessage]:
    """Copy of `message` cut to half its length (not below `min_chars`), or None."""
    text = _tool_text(message.content)
    if text is None:
        return None
    total = len(text)
    note = _NOTE_RE.search(text)
    if note:
        text = text[: note.start()]
        total = int(note.group(1))
    if len(text) <= min_chars:
        return None
    kept = max(min_chars, len(text) // 2)
    content = text[:kept] + _NOTE.format(kept=kept, total=total)
    return message.model_copy(update={"content": content})


def shrink_tool_results(
    messages: Sequence[BaseMessage], min_chars: int = MIN_TOOL_RESULT_CHARS
) -> Optional[List[BaseMessage]]:
    """
    Return a copy of `messages` with older tool results cut to half their
    length (not below `min_chars`), or None if nothing could be cut. The
    newest tool result is only cut once the older ones are at the floor.
    """
    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    for candidates in (tool_indices[:-1], tool_indices[-1:]):
        shrunk = list(messages)
        changed = False
        for i in candidates:
            shorter = _shorten(messages[i], min_chars)
            if shorter is not None:
                shrunk[i] = shorter
                changed = True
        if changed:
            return shrunk
    return None


class ShrinkToolResultsOnOverflow(AgentMiddleware):
    """On a context-overflow error, shorten older tool results and retry the model call."""

    def __init__(
        self,
        is_overflow: Callable[[Exception], bool],
        max_rounds: int = MAX_SHRINK_ROUNDS,
    ):
        super().__init__()
        self._is_overflow = is_overflow
        self._max_rounds = max_rounds

    def _shrunk_request(self, request: Any, exc: Exception) -> Optional[Any]:
        """The request with shorter tool results, or None if `exc` should propagate."""
        if not self._is_overflow(exc):
            return None
        messages = shrink_tool_results(request.messages)
        if messages is None:
            return None
        logger.warning(
            "Model call exceeded the context window; shortened older tool results and retrying."
        )
        return request.override(messages=messages)

    def wrap_model_call(self, request, handler):
        for _ in range(self._max_rounds):
            try:
                return handler(request)
            except Exception as exc:
                shrunk = self._shrunk_request(request, exc)
                if shrunk is None:
                    raise
                request = shrunk
        return handler(request)

    async def awrap_model_call(self, request, handler):
        for _ in range(self._max_rounds):
            try:
                return await handler(request)
            except Exception as exc:
                shrunk = self._shrunk_request(request, exc)
                if shrunk is None:
                    raise
                request = shrunk
        return await handler(request)
