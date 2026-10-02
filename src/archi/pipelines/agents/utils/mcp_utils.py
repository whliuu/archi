from typing import Optional, Any, Dict, List, Sequence, Tuple
import asyncio
import re
import threading
from src.utils.env import read_secret
from src.utils.logging import get_logger

logger = get_logger(__name__)

_SECRET_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_header_secrets(headers: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Expand ${SECRET_NAME} references in MCP header values from the deployment
    secrets (read_secret: *_FILE or env var), so tokens stay out of config.yaml
    and the static_config table.

    Returns:
        resolved: headers with references substituted
        missing: secret names that were referenced but not set
    """
    missing: List[str] = []

    def _sub(match: re.Match) -> str:
        value = read_secret(match.group(1))
        if not value:
            missing.append(match.group(1))
        return value

    resolved = {
        k: _SECRET_REF.sub(_sub, v) if isinstance(v, str) else v
        for k, v in headers.items()
    }
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
        return future.result(timeout=timeout)

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
