"""Direct ACP runtime consumers honor the companion engine-binding seam."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.client import PROTOCOL_VERSION_CLAUDE
from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, METHOD_SESSION_NEW, acp_client_capabilities
from kiro_crew.platform import acp_binding


class _BoundRegistry:
    def agent_client_binding(self, agent_name: str) -> dict[str, object]:
        assert agent_name == "kirocrew-lite"
        return {
            "acp_backend": ACP_BACKEND_CLAUDE,
            "extra_env": {"API_TIMEOUT_MS": 300000},
            "model": "fast",
            "model_switch_method": "session_set_model",
            "sandbox_mode": "off",
        }


@pytest.mark.parametrize("error_type", [RuntimeError, AttributeError, TypeError])
@pytest.mark.parametrize("source", ["context", "registry"])
def test_runtime_binding_does_not_switch_engines_after_lookup_failure(
    monkeypatch, error_type, source
):
    def fail(*args):
        raise error_type("binding unavailable")

    if source == "context":
        monkeypatch.setattr(acp_binding, "current_context", fail)
    else:
        monkeypatch.setattr(
            acp_binding,
            "current_context",
            lambda: SimpleNamespace(providers=SimpleNamespace(agent_client_binding=fail)),
        )
    with pytest.raises(error_type, match="binding unavailable"):
        acp_binding.runtime_client_binding("kirocrew-lite")


def test_runtime_binding_is_empty_for_the_default_registry(monkeypatch):
    from kiro_crew.platform.defaults import DefaultProviderRegistry

    monkeypatch.setattr(
        acp_binding,
        "current_context",
        lambda: SimpleNamespace(providers=DefaultProviderRegistry()),
    )

    assert acp_binding.runtime_client_binding("kirocrew-lite") == {}


def test_runtime_binding_is_whitelisted_and_preserves_caller_settings(monkeypatch):
    monkeypatch.setattr(
        acp_binding,
        "current_context",
        lambda: SimpleNamespace(providers=_BoundRegistry()),
    )

    binding = acp_binding.runtime_client_binding("kirocrew-lite")
    assert binding == {
        "acp_backend": ACP_BACKEND_CLAUDE,
        "extra_env": {"API_TIMEOUT_MS": "300000"},
        "model": "fast",
        "model_switch_method": "session_set_model",
    }

    runtime_kwargs = {
        "sandbox_mode": "auto",
        "work_dir": "/work/review",
        "extra_env": {"CALLER_OWNED": "1"},
    }
    acp_binding.apply_runtime_client_binding(runtime_kwargs, binding)

    assert runtime_kwargs == {
        "acp_backend": ACP_BACKEND_CLAUDE,
        "extra_env": {"CALLER_OWNED": "1", "API_TIMEOUT_MS": "300000"},
        "model": "fast",
        "model_switch_method": "session_set_model",
        "sandbox_mode": "auto",
        "work_dir": "/work/review",
    }
    assert binding["extra_env"] == {"API_TIMEOUT_MS": "300000"}


async def _plan_claude_spawn(runtime: AcpRuntime, monkeypatch) -> None:
    """Resolve the runtime's spawn plan through the claude harness, binary pinned.

    The model and the ``claudeCode`` envelope are what the harness's plan says
    every session on the process needs, so a session started without the plan
    having run would test a runtime no production caller can build.
    """
    from kiro_crew.acp.harness import claude as claude_mod

    monkeypatch.setattr(claude_mod, "_resolve_claude_acp_bin", lambda: (["claude-agent-acp"], ""))
    await runtime._resolve_spawn_plan()


@pytest.mark.asyncio
async def test_claude_runtime_uses_bound_session_set_model(monkeypatch):
    runtime = AcpRuntime(
        work_dir="/tmp",
        acp_backend=ACP_BACKEND_CLAUDE,
        model="fast",
        model_switch_method="session_set_model",
    )
    await _plan_claude_spawn(runtime, monkeypatch)
    runtime._initialized = True
    runtime._send_and_await = AsyncMock(return_value={"sessionId": "claude-session"})

    with (
        patch.object(AcpSessionHandle, "drain_init", new=AsyncMock()),
        patch.object(AcpSessionHandle, "set_model", new=AsyncMock()) as set_model,
        patch.object(AcpSessionHandle, "set_config_option", new=AsyncMock()) as set_option,
    ):
        await runtime.create_session(mcp_servers=[])

        runtime._send_and_await.assert_awaited_once_with(
            METHOD_SESSION_NEW,
            {"cwd": "/tmp", "mcpServers": [], "_meta": {"claudeCode": {"options": {}}}},
            timeout=90.0,
        )
    set_model.assert_awaited_once_with("fast")
    set_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_claude_runtime_uses_config_option_without_bound_switch_method(monkeypatch):
    runtime = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CLAUDE, model="fast")
    await _plan_claude_spawn(runtime, monkeypatch)
    runtime._initialized = True
    runtime._send_and_await = AsyncMock(return_value={"sessionId": "claude-session"})

    with (
        patch.object(AcpSessionHandle, "drain_init", new=AsyncMock()),
        patch.object(AcpSessionHandle, "set_model", new=AsyncMock()) as set_model,
        patch.object(AcpSessionHandle, "set_config_option", new=AsyncMock()) as set_option,
    ):
        await runtime.create_session(mcp_servers=[])

    set_option.assert_awaited_once_with("model", "fast")
    set_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_claude_runtime_uses_claude_handshake_and_environment(monkeypatch, tmp_path):
    """Shared Claude sessions use the adapter protocol and native executable."""
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.client import CLIENT_NAME, CLIENT_VERSION
    from kiro_crew.acp.harness import claude as claude_mod

    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    monkeypatch.setattr(claude_mod, "_resolve_claude_acp_bin", lambda: (["claude-agent-acp"], ""))
    monkeypatch.setattr(claude_mod, "_resolve_claude_code_executable", lambda: "/usr/bin/claude")
    monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: False)
    plan = await runtime._resolve_spawn_plan()
    assert plan.argv == ["claude-agent-acp"]

    env = {"KIRO_API_KEY": "test-key"}
    runtime._harness.apply_spawn_env(env)
    assert "KIRO_API_KEY" not in env
    assert env["CLAUDE_CODE_EXECUTABLE"] == "/usr/bin/claude"

    handshake = AsyncMock(return_value={"agentCapabilities": {}})
    monkeypatch.setattr(runtime, "_send_and_await", handshake)
    await runtime._initialize_handshake(runtime._harness.client_capabilities)
    handshake.assert_awaited_once_with(
        "initialize",
        {
            "clientCapabilities": acp_client_capabilities(ACP_BACKEND_CLAUDE),
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "protocolVersion": PROTOCOL_VERSION_CLAUDE,
        },
        timeout=runtime_mod._INITIALIZE_TIMEOUT,
    )
