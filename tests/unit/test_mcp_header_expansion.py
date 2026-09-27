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
