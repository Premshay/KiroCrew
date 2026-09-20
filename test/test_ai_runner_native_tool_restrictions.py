"""Restricted runs bind the native tool surface before a prompt can execute."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO, EVENT_COMPLETE
from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import SessionAgentRunner
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider


@pytest.mark.parametrize("allowed", [[], ["Read", "Grep", "Glob"]])
@pytest.mark.parametrize("substitute", [False, True])
def test_runner_binds_native_restrictions_before_prompt(tmp_path, monkeypatch, allowed, substitute):
    provider = AcpProvider(
        work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, permission_mode="bypassPermissions"
    )
    client = provider._client
    wire = []

    async def send(method, params):
        wire.append((method, params))
        return len(wire)

    async def response(request_id, **kwargs):
        if substitute and request_id == 1:
            client._last_substitution_model = "replacement"
            return {}
        return {"sessionId": "restricted"}

    monkeypatch.setattr(client, "_send_request", send)
    monkeypatch.setattr(client, "_wait_for_response", response)
    monkeypatch.setattr(client, "_session_work_dir", AsyncMock(return_value=str(tmp_path)))
    monkeypatch.setattr(client, "_write_claude_local_settings", Mock())
    monkeypatch.setattr(client, "_begin_session_report", Mock())
    monkeypatch.setattr(client, "_guard_unresolved_mcp_refs", Mock())
    # Each source would expose a shell-capable MCP tool if restriction is lost.
    for source in (
        "_translated_session_mcp_servers",
        "_claude_session_mcp_servers",
        "_session_capability_mcp_servers",
        "_pooled_mcp_servers",
    ):
        monkeypatch.setattr(client, source, Mock(side_effect=AssertionError("MCP mounted")))

    async def start():
        assert await client._new_session_following_substitution() == {"sessionId": "restricted"}

    async def stream(prompt):
        assert len(wire) == (2 if substitute else 1)
        for method, params in wire:
            assert method == "session/new"
            assert params["mcpServers"] == []
            options = params["_meta"]["claudeCode"]["options"]
            assert options["tools"] == allowed
            assert "Bash" not in options["tools"]
            assert options["strictMcpConfig"] is True
            assert options["disallowedTools"] == ["mcp__*"]
            assert client._permission_mode == "bypassPermissions"
            assert "permissionMode" not in options
            assert "allowedTools" not in options
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    monkeypatch.setattr(provider, "start", start)
    monkeypatch.setattr(provider, "stream", stream)
    shutdown = AsyncMock()
    monkeypatch.setattr(provider, "shutdown", shutdown)
    runner = SessionAgentRunner(provider_factory=lambda key, **kwargs: provider)
    result = runner.run("inspect", cwd=str(tmp_path), allowed_tools=allowed)
    assert result.ok, result.error
    shutdown.assert_awaited_once()


@pytest.mark.parametrize("allowed", [[], ["Read"]])
def test_unsupported_provider_refuses_before_start(tmp_path, allowed):
    provider = SimpleNamespace(
        restrict_tools=lambda tools: LLMProvider.restrict_tools(None, tools),
        start=AsyncMock(),
        shutdown=AsyncMock(),
        stream=Mock(),
    )
    result = SessionAgentRunner(provider_factory=lambda key, **kwargs: provider).run(
        "inspect", cwd=str(tmp_path), allowed_tools=allowed
    )
    assert not result.ok
    assert "cannot enforce a tool whitelist" in result.error
    provider.start.assert_not_awaited()
    provider.stream.assert_not_called()
    provider.shutdown.assert_awaited_once()


def test_restricted_factory_error_never_retries_without_agent_or_cwd(tmp_path):
    factory = Mock(side_effect=TypeError("factory rejected scoped arguments"))
    result = SessionAgentRunner(provider_factory=factory).run(
        "inspect", cwd=str(tmp_path), allowed_tools=[]
    )
    assert not result.ok
    assert "factory rejected scoped arguments" in result.error
    factory.assert_called_once()
    assert factory.call_args.kwargs["cwd"] == str(tmp_path)
    assert factory.call_args.kwargs["agent"] == "auto-improvement-discovery"


def test_unrestricted_provider_needs_no_restriction_support(tmp_path):
    async def stream(prompt):
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    provider = SimpleNamespace(start=AsyncMock(), shutdown=AsyncMock(), stream=stream)
    result = SessionAgentRunner(provider_factory=lambda key, **kwargs: provider).run(
        "inspect", cwd=str(tmp_path)
    )
    assert result.ok, result.error
    provider.start.assert_awaited_once()


@pytest.mark.parametrize("allowed", [["default"], ["*"], ["Read", "mcp__server__tool"], [None]])
def test_claude_rejects_nonliteral_or_unsupported_whitelists(tmp_path, allowed):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    with pytest.raises(ValueError, match="exact file/shell tool names"):
        provider.restrict_tools(allowed)


def test_other_backend_cannot_silently_ignore_restrictions(tmp_path):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_KIRO)
    with pytest.raises(NotImplementedError, match="cannot enforce"):
        provider.restrict_tools([])


def test_restrictions_are_copied_and_cannot_be_changed_after_start(tmp_path):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    allowed = ["Read"]
    provider.restrict_tools(allowed)
    allowed.append("Bash")
    assert provider._client._claude_session_meta()["claudeCode"]["options"]["tools"] == ["Read"]
    provider._client._session_id = "active"
    with pytest.raises(RuntimeError, match="before provider startup"):
        provider.restrict_tools(["Bash"])


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [[], ["Read", "Grep", "Glob"]])
async def test_session_load_carries_the_same_native_constraints(tmp_path, monkeypatch, allowed):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    provider.restrict_tools(allowed)
    client = provider._client
    client._resume_session_id = "saved"
    wire = {}

    async def send(method, params):
        wire[method] = params
        if method == "session/load":
            raise asyncio.CancelledError
        return 1

    monkeypatch.setattr(client, "_send_request", send)
    monkeypatch.setattr(
        client,
        "_wait_for_response",
        AsyncMock(return_value={"agentCapabilities": {"loadSession": True}}),
    )
    monkeypatch.setattr(client, "_session_work_dir", AsyncMock(return_value=str(tmp_path)))
    with pytest.raises(asyncio.CancelledError):
        await client._initialize_session()
    params = wire["session/load"]
    assert params["mcpServers"] == []
    assert params["_meta"]["claudeCode"]["options"] == {
        "tools": allowed,
        "strictMcpConfig": True,
        "disallowedTools": ["mcp__*"],
    }


def test_unspecified_claude_options_preserve_existing_behavior(tmp_path, monkeypatch):
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    monkeypatch.setattr(provider._client, "_session_mcp_servers", lambda: [])
    assert provider._client._claude_session_meta()["claudeCode"]["options"] == {}
