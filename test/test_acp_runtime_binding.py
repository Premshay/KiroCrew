"""Direct ACP runtime consumers honor the companion engine-binding seam."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.client import PROTOCOL_VERSION_CLAUDE
from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_CLIENT_CAPABILITIES, METHOD_SESSION_NEW
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


@pytest.mark.asyncio
async def test_claude_runtime_uses_bound_session_set_model():
    runtime = AcpRuntime(
        work_dir="/tmp",
        acp_backend=ACP_BACKEND_CLAUDE,
        model="fast",
        model_switch_method="session_set_model",
    )
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
async def test_claude_runtime_uses_config_option_without_bound_switch_method():
    runtime = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CLAUDE, model="fast")
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
async def test_claude_runtime_spawn_uses_claude_adapter_protocol(monkeypatch, tmp_path):
    """Direct runtimes must not send Kiro's incompatible handshake to Claude."""
    runtime = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CLAUDE)
    process = SimpleNamespace(pid=12345, stdout=object(), stderr=object())
    create_process = AsyncMock(return_value=process)
    handshake = AsyncMock(return_value={"agentCapabilities": {}})
    wrapped: dict[str, object] = {}

    def wrap(argv, **kwargs):
        wrapped["argv"] = argv
        wrapped.update(kwargs)
        return argv, None

    async def no_reader():
        return None

    monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
    monkeypatch.setattr("kiro_crew.acp.runtime._resolve_claude_acp_bin", lambda: ["claude-agent-acp"])
    monkeypatch.setattr("kiro_crew.acp.runtime._resolve_claude_code_executable", lambda: "/usr/bin/claude")
    monkeypatch.setattr("kiro_crew.acp.runtime.wrap_argv", wrap)
    monkeypatch.setattr("kiro_crew.acp.runtime.cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr("kiro_crew.acp.runtime.create_subprocess_limited", create_process)
    monkeypatch.setattr("kiro_crew.acp.runtime._track_pid", lambda _pid: None)
    monkeypatch.setattr("kiro_crew.acp.runtime._track_session_pid", lambda _pid: None)
    monkeypatch.setattr("kiro_crew.acp.runtime.register_protected_pid", lambda _pid: None)
    monkeypatch.setattr("kiro_crew.acp.runtime._get_start_time", lambda _pid: None)
    monkeypatch.setattr(runtime, "_reader_loop", no_reader)
    monkeypatch.setattr(runtime, "_drain_stderr", no_reader)
    monkeypatch.setattr(runtime, "_send_and_await", handshake)

    await runtime.spawn()

    assert wrapped == {
        "argv": ["claude-agent-acp"],
        "is_kiro_cli": None,
        "mode": "auto",
        "strip_python_env": True,
    }
    assert create_process.await_args.args == ("claude-agent-acp",)
    assert create_process.await_args.kwargs["env"]["CLAUDE_CODE_EXECUTABLE"] == "/usr/bin/claude"
    handshake.assert_awaited_once_with(
        "initialize",
        {
            "clientCapabilities": ACP_CLIENT_CAPABILITIES,
            "clientInfo": {"name": "kirocrew", "version": "0.1.2"},
            "protocolVersion": PROTOCOL_VERSION_CLAUDE,
        },
    )
