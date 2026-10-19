import asyncio
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.utils.env import read_secret
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ${NAME}, or ${NAME:-default} to fall back to `default` when NAME is unset.
_SECRET_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def resolve_secret_refs(text: str) -> Tuple[str, List[str]]:
    """
    Expand ${SECRET_NAME} and ${SECRET_NAME:-default} references in `text`
    from the deployment secrets (read_secret: *_FILE or env var), so tokens
    and per-host values stay out of config.yaml and the static_config table.

    Returns:
        resolved: text with references substituted
        missing: secret names referenced without a default but not set
    """
    missing: List[str] = []

    def _sub(match: re.Match) -> str:
        value = read_secret(match.group(1))
        if value:
            return value
        if match.group(2) is not None:
            return match.group(2)
        missing.append(match.group(1))
        return ""

    return _SECRET_REF.sub(_sub, text), missing


def resolve_header_secrets(headers: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Expand secret references (see resolve_secret_refs) in MCP header values.

    Returns:
        resolved: headers with references substituted
        missing: secret names that were referenced but not set
    """
    missing: List[str] = []
    resolved: Dict[str, Any] = {}
    for k, v in headers.items():
        if isinstance(v, str):
            v, value_missing = resolve_secret_refs(v)
            missing.extend(value_missing)
        resolved[k] = v
    return resolved, missing


_MCP_TOOL = "mcp"
_MCP_SERVER_PREFIX = "mcp:"


def uses_mcp(tool_names: Sequence[str]) -> bool:
    """True if an agent's tool list has `mcp` or any `mcp:<server>` entry."""
    return any(n == _MCP_TOOL or n.startswith(_MCP_SERVER_PREFIX) for n in tool_names)


def mcp_servers_for_agent(tool_names: Sequence[str]) -> Optional[List[str]]:
    """
    MCP servers an agent's tool list asks for: `mcp` means every configured
    server (returns None), `mcp:<server>` entries name specific servers.
    """
    if _MCP_TOOL in tool_names:
        return None
    return [n[len(_MCP_SERVER_PREFIX):] for n in tool_names if n.startswith(_MCP_SERVER_PREFIX)]


def select_mcp_servers(
    servers: Dict[str, Any], names: Optional[Sequence[str]]
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Restrict the configured MCP servers to `names` (None keeps all).

    Returns:
        selected: server configs for the requested names
        unknown: requested names with no configured server
    """
    if names is None:
        return dict(servers), []
    selected = {n: servers[n] for n in names if n in servers}
    unknown = [n for n in names if n not in servers]
    return selected, unknown


def filter_mcp_tools(tools: Sequence[Any], include: Optional[Sequence[str]]) -> Tuple[List[Any], List[str]]:
    """
    Keep only the tools named in a server's `include_tools` allow-list
    (None keeps all; an empty list keeps none). Names are the server's
    original tool names, before archi's `<server>__` prefix.

    Returns:
        kept: tools on the allow-list
        missing: allow-listed names the server does not serve
    """
    if include is None:
        return list(tools), []
    allowed = set(include)
    served = {t.name for t in tools}
    kept = [t for t in tools if t.name in allowed]
    missing = [n for n in include if n not in served]
    return kept, missing


# Default cap on one MCP tool result, in characters (roughly 10k tokens). Every
# result stays in the agent's context for the rest of the turn, so a few
# oversized results (e.g. knowledge-base searches) can overflow the model's
# context window.
DEFAULT_MCP_MAX_OUTPUT_CHARS = 40000


def truncate_tool_text(content: Any, max_chars: int) -> Any:
    """
    Cut an MCP tool's text result to `max_chars`, ending with a note so the LLM
    knows it was cut. The adapter returns a str, or a list of str for
    multi-part results (joined before cutting). A non-positive `max_chars`
    disables the cap.
    """
    if max_chars <= 0:
        return content
    text = "\n".join(content) if isinstance(content, list) else content
    if not isinstance(text, str) or len(text) <= max_chars:
        return content
    return (
        f"{text[:max_chars]}\n\n[Output truncated: showing the first {max_chars} of "
        f"{len(text)} characters. Narrow the query or fetch a single item to see the rest.]"
    )


def cap_tool_output(tool: Any, max_chars: int) -> None:
    """
    Wrap an MCP tool's coroutine so its text result is cut to `max_chars`.
    Both the async path and archi's sync wrapper call `tool.coroutine`, so this
    covers every call. Tools without a coroutine are left alone.
    """
    call = getattr(tool, "coroutine", None)
    if call is None or max_chars <= 0:
        return

    async def capped_call(*args, **kwargs):
        content, artifact = await call(*args, **kwargs)
        return truncate_tool_text(content, max_chars), artifact

    tool.coroutine = capped_call

class AsyncLoopThread:
    """
    A dedicated background thread running a single event loop.

    This ensures all async operations (MCP client init, tool calls) happen
    on the same event loop, preventing ClosedResourceError.

    Usage:
        runner = AsyncLoopThread.get_instance()
        result = runner.run(some_async_coroutine())
    """

    _instance: Optional["AsyncLoopThread"] = None
    _lock = threading.Lock()

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._started = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="mcp-async-loop"
        )
        self.thread.start()

        # Wait for the loop to actually start before returning
        if not self._started.wait(timeout=10.0):
            raise RuntimeError("Failed to start async loop thread")
        logger.info("Background async loop started for MCP operations")

    def _run(self):
        """Run the event loop forever in the background thread."""
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    def run(self, coro, timeout: Optional[float] = 120.0) -> Any:
        """
        Schedule a coroutine on the background loop and wait for result.

        Args:
            coro: An awaitable coroutine
            timeout: Maximum seconds to wait (default 120s for MCP operations)

        Returns:
            The result of the coroutine

        Raises:
            TimeoutError: If the coroutine doesn't complete in time
            Any exception raised by the coroutine
        """
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise

    def in_loop_thread(self) -> bool:
        """Return True if called from the background event-loop thread."""
        return threading.current_thread() is self.thread
        # or: return threading.get_ident() == self.thread.ident

    @classmethod
    def get_instance(cls) -> "AsyncLoopThread":
        """Get or create the singleton async runner instance."""
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern for thread safety
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def shutdown(self):
        """Gracefully shutdown the background loop."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5.0)
        logger.info("Background async loop stopped")
