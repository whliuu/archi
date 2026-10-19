from src.archi.pipelines.agents.tools.mcp import build_client_config
from src.archi.pipelines.agents.utils.mcp_utils import resolve_header_secrets


def test_expands_secret_from_env(monkeypatch):
    monkeypatch.delenv("MCP_TEST_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MCP_TEST_TOKEN", "abc123")
    resolved, missing = resolve_header_secrets({"Authorization": "Bearer ${MCP_TEST_TOKEN}"})
    assert resolved == {"Authorization": "Bearer abc123"}
    assert missing == []


def test_expands_secret_from_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "token.txt"
    secret_file.write_text("fromfile\n")
    monkeypatch.setenv("MCP_TEST_TOKEN_FILE", str(secret_file))
    resolved, missing = resolve_header_secrets({"Authorization": "Bearer ${MCP_TEST_TOKEN}"})
    assert resolved == {"Authorization": "Bearer fromfile"}
    assert missing == []


def test_reports_missing_secret(monkeypatch):
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    monkeypatch.delenv("MCP_TEST_TOKEN_FILE", raising=False)
    _, missing = resolve_header_secrets({"Authorization": "Bearer ${MCP_TEST_TOKEN}"})
    assert missing == ["MCP_TEST_TOKEN"]


def test_leaves_plain_headers_untouched():
    resolved, missing = resolve_header_secrets({"X-Static": "value", "X-Num": 3})
    assert resolved == {"X-Static": "value", "X-Num": 3}
    assert missing == []


def test_uses_default_when_secret_is_unset(monkeypatch):
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    monkeypatch.delenv("MCP_TEST_TOKEN_FILE", raising=False)
    resolved, missing = resolve_header_secrets({"X-Host": "${MCP_TEST_TOKEN:-fallback}"})
    assert resolved == {"X-Host": "fallback"}
    assert missing == []


def test_secret_overrides_default(monkeypatch):
    monkeypatch.delenv("MCP_TEST_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MCP_TEST_TOKEN", "set")
    resolved, _ = resolve_header_secrets({"X-Host": "${MCP_TEST_TOKEN:-fallback}"})
    assert resolved == {"X-Host": "set"}


# A per-host URL (e.g. the daq server, which a container on the same host must
# reach via host.containers.internal) comes from that host's secrets.env.
DAQ_URL = "${MCP_TEST_URL:-http://daq.example:8765/mcp}"


def test_url_secret_is_expanded(monkeypatch):
    monkeypatch.delenv("MCP_TEST_URL_FILE", raising=False)
    monkeypatch.setenv("MCP_TEST_URL", "http://host.containers.internal:8765/mcp")
    cfg, missing = build_client_config({"transport": "streamable_http", "url": DAQ_URL})
    assert cfg["url"] == "http://host.containers.internal:8765/mcp"
    assert missing == []


def test_url_falls_back_to_its_default(monkeypatch):
    monkeypatch.delenv("MCP_TEST_URL", raising=False)
    monkeypatch.delenv("MCP_TEST_URL_FILE", raising=False)
    cfg, missing = build_client_config({"transport": "streamable_http", "url": DAQ_URL})
    assert cfg["url"] == "http://daq.example:8765/mcp"
    assert missing == []


def test_url_secret_without_default_is_reported_missing(monkeypatch):
    monkeypatch.delenv("MCP_TEST_URL", raising=False)
    monkeypatch.delenv("MCP_TEST_URL_FILE", raising=False)
    _, missing = build_client_config({"transport": "streamable_http", "url": "${MCP_TEST_URL}"})
    assert missing == ["MCP_TEST_URL"]
