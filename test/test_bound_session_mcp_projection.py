"""Host admission and an empty spec projection are distinct MCP bind facts."""

from unittest.mock import Mock

from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE


def _client(tmp_path, monkeypatch, backend=ACP_BACKEND_GOOSE):
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    # The bind fixtures supply an admitted projection without running a handshake.
    client._session_mcp_withheld = False
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [])
    monkeypatch.setattr(client, "_session_capability_mcp_servers", lambda: [])
    return client


def test_nonarray_host_never_calls_an_overridden_reader_or_resolves_spec(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, backend="")
    reader = Mock(side_effect=AssertionError("nonarray host consulted the spec reader"))
    resolver = Mock(side_effect=AssertionError("nonarray host resolved a spec"))
    monkeypatch.setattr(client, "_session_mcp_servers", reader)
    monkeypatch.setattr(client, "_resolve_session_mcp_servers", resolver)
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [{"name": "pooled"}])

    assert client._bound_session_mcp_servers() == [{"name": "pooled"}]
    reader.assert_not_called()
    resolver.assert_not_called()


def test_empty_accessor_override_still_keeps_the_pooled_half(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    reader = Mock(return_value=[])
    monkeypatch.setattr(client, "_session_mcp_servers", reader)
    monkeypatch.setattr(
        client,
        "_resolve_session_mcp_servers",
        Mock(side_effect=AssertionError("an overridden reader must not resolve the cache")),
    )
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [{"name": "pooled"}])

    assert client._bound_session_mcp_servers() == [{"name": "pooled"}]
    assert reader.called


def test_cold_projection_resolves_once_and_honors_the_named_harness_override(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resolver = Mock(return_value=[{"name": "spec"}])
    monkeypatch.setattr(client, "_resolve_session_mcp_servers", resolver)
    monkeypatch.setattr(client, "_goose_session_mcp_servers", lambda: [{"name": "override"}])

    assert client._bound_session_mcp_servers() == [{"name": "override"}]
    assert client._session_mcp_servers() == [{"name": "spec"}]
    resolver.assert_called_once_with()


def test_tool_whitelist_does_not_read_or_mount_either_half(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client._allowed_tools = ("Read",)
    reader = Mock(side_effect=AssertionError("a whitelisted session read the MCP projection"))
    monkeypatch.setattr(client, "_session_mcp_servers", reader)
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [{"name": "pooled"}])

    assert client._bound_session_mcp_servers() == []
    reader.assert_not_called()


def test_unowned_claude_permission_surface_withholds_the_pooled_half(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, backend=ACP_BACKEND_CLAUDE)
    client._session_mcp_withheld = True
    monkeypatch.setattr(AcpClient, "_permission_surface_governed", property(lambda _: False))
    monkeypatch.setattr(client, "_session_mcp_servers", lambda: [])
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [{"name": "pooled"}])

    assert client._bound_session_mcp_servers() == []
