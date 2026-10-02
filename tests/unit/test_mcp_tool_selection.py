from types import SimpleNamespace

from src.archi.pipelines.agents.utils.mcp_utils import (
    filter_mcp_tools,
    mcp_servers_for_agent,
    select_mcp_servers,
    uses_mcp,
)


def _tools(*names):
    return [SimpleNamespace(name=n) for n in names]


def test_uses_mcp_for_plain_and_server_entries():
    assert uses_mcp(["search_local_files", "mcp"])
    assert uses_mcp(["mcp:daq"])
    assert not uses_mcp(["search_local_files", "mcpx"])


def test_plain_mcp_selects_all_servers():
    assert mcp_servers_for_agent(["search_local_files", "mcp"]) is None


def test_server_entries_select_named_servers_in_order():
    assert mcp_servers_for_agent(["mcp:runs", "search_local_files", "mcp:arxiv"]) == ["runs", "arxiv"]


def test_plain_mcp_wins_over_server_entries():
    assert mcp_servers_for_agent(["mcp:daq", "mcp"]) is None


def test_select_servers_none_keeps_all():
    servers = {"arxiv": {"url": "a"}, "daq": {"url": "d"}}
    selected, unknown = select_mcp_servers(servers, None)
    assert selected == {"arxiv": {"url": "a"}, "daq": {"url": "d"}}
    assert unknown == []


def test_select_servers_keeps_only_named():
    servers = {"arxiv": {"url": "a"}, "daq": {"url": "d"}}
    selected, unknown = select_mcp_servers(servers, ["daq"])
    assert selected == {"daq": {"url": "d"}}
    assert unknown == []


def test_select_servers_reports_unknown_names():
    selected, unknown = select_mcp_servers({"arxiv": {"url": "a"}}, ["daq"])
    assert selected == {}
    assert unknown == ["daq"]


def test_filter_without_include_list_keeps_all_tools():
    kept, missing = filter_mcp_tools(_tools("get_current_state", "run_fe_macro"), None)
    assert [t.name for t in kept] == ["get_current_state", "run_fe_macro"]
    assert missing == []


def test_filter_drops_tools_not_in_include_list():
    kept, missing = filter_mcp_tools(
        _tools("get_current_state", "run_fe_macro", "read_log"),
        ["get_current_state", "read_log"],
    )
    assert [t.name for t in kept] == ["get_current_state", "read_log"]
    assert missing == []


def test_filter_reports_listed_tools_the_server_does_not_serve():
    _, missing = filter_mcp_tools(_tools("get_current_state"), ["get_current_state", "renamed_tool"])
    assert missing == ["renamed_tool"]


def test_filter_empty_include_list_keeps_nothing():
    kept, _ = filter_mcp_tools(_tools("get_current_state", "run_fe_macro"), [])
    assert kept == []
