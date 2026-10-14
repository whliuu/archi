"""Serve the daqpy MCP tools read-only over HTTP for the mu2e DAQ agent.

Targets daqpy's develop branch (MCPServer, mcp 2.x), which is what runs on
mu2e-dl-02. `daqpy-mcp` has no authentication, and besides the read tools it
serves send_chat (posts to the operators' OTS chat, optionally as a popup to
every OTS user) and tools that put their arguments into remote shell commands
(search_logs, read_run_record_file, read_artdaq_log's host/path mode). This
serves only the read tools, with read_artdaq_log limited to process labels.

Run with daqpy's venv, after `source setup_ots.sh shift`:

    env PYTHONPATH= <daqpy>/.venv/bin/python daqpy_mcp_readonly.py --host 0.0.0.0
"""

import argparse
import logging
import socket
from typing import Any

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from daqpy.mcp.server import build_mcp
from daqpy.mcp.tools.artdaq import read_artdaq_log as _read_artdaq_log

# Keep in sync with mcp_servers.daq.include_tools in configs/mu2e/config.yaml.
# The runlog_* tools only exist when the run-info DB is configured.
READ_TOOLS = {
    "get_app_status",
    "get_current_state",
    "get_system_messages",
    "search_messages",
    "list_message_sources",
    "list_log_files",
    "read_log",
    "list_artdaq_processes",
    "read_artdaq_log",
    "list_artdaq_fcl_files",
    "get_artdaq_stats",
    "list_run_records",
    "get_run_record",
    "get_fe_macro_list",
    "list_docs",
    "read_doc",
    "runlog_find",
    "runlog_run",
    "runlog_config",
}

log = logging.getLogger("daqpy_mcp_readonly")


def read_artdaq_log(
    label: str, tail: int | None = 100, art: bool = False, subsystem: str | None = None
) -> dict[str, Any]:
    """Return the log of a current-run artdaq process.

    ``label`` comes from ``list_artdaq_processes``; ``art=True`` reads the
    art-framework log(s) instead of the main log. ``tail`` lines from the end,
    at most 2000 (default 100).
    """
    # Upstream also takes host + path for past runs, which reaches a remote shell.
    return _read_artdaq_log(label=label, tail=tail, art=art, subsystem=subsystem)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="Bind address.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Extra hostname clients may use to reach this server (repeatable).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    mcp = build_mcp(allow_fe_macros=False, runlog_read_only=True)
    served = {tool.name for tool in mcp._tool_manager.list_tools()}
    for name in served - READ_TOOLS:
        mcp.remove_tool(name)
    if "read_artdaq_log" in served:
        mcp.remove_tool("read_artdaq_log")
        mcp.tool()(read_artdaq_log)
    log.info("Serving %d read-only tools; removed: %s", len(served & READ_TOOLS), sorted(served - READ_TOOLS))
    if READ_TOOLS - served:
        log.info("Not provided by this daqpy: %s", sorted(READ_TOOLS - served))

    fqdn = socket.getfqdn()
    hosts = sorted({"localhost", "127.0.0.1", "host.containers.internal", fqdn, fqdn.split(".")[0], *args.allow_host})
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in hosts],
        allowed_origins=[f"http://{h}:*" for h in hosts],
    )
    log.info("Accepting Host headers: %s", hosts)

    app = mcp.streamable_http_app(host=args.host, transport_security=security)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
