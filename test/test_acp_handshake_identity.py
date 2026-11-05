"""A dispatcher cannot serve another known harness under the selected policy."""

from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.handshake_identity import refuse_conflicting_handshake
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KAS


def _codex_handshake():
    return {
        "protocolVersion": 1,
        "agentInfo": {
            "name": "@agentclientprotocol/codex-acp",
            "version": "2.2.2",
        },
    }


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"agentInfo": None},
        {"agentInfo": {"name": []}},
        {"agentInfo": {"name": "operator-adapter"}},
    ],
)
def test_an_unrecognized_identity_does_not_invent_a_backend(result):
    refuse_conflicting_handshake(ACP_BACKEND_KAS, result)


def test_the_selected_adapter_is_accepted():
    refuse_conflicting_handshake(ACP_BACKEND_CODEX, _codex_handshake())


@pytest.mark.asyncio
async def test_direct_client_refuses_before_session_creation_and_does_not_retry(
    tmp_path, monkeypatch
):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    send = AsyncMock(return_value=1)
    spawn = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(client, "_send_request", send)
    monkeypatch.setattr(client, "_wait_for_response", AsyncMock(return_value=_codex_handshake()))
    monkeypatch.setattr(client, "_spawn", spawn)
    monkeypatch.setattr(client, "_cleanup_failed_live_spawn", cleanup)
    monkeypatch.setattr(client, "_reset_state", MagicMock())
    with pytest.raises(AcpToolGateUnroutable, match="no session was opened"):
        await client.ensure_ready()
    send.assert_awaited_once_with("initialize", ANY)
    spawn.assert_awaited_once()
    cleanup.assert_awaited_once()
    assert not client._session_id
    assert not client._agent_version_read


@pytest.mark.asyncio
async def test_shared_runtime_refuses_the_same_initialize_before_adopting_capabilities(
    tmp_path, monkeypatch
):
    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    send = AsyncMock(return_value=_codex_handshake())
    monkeypatch.setattr(runtime, "_send_and_await", send)
    with pytest.raises(AcpToolGateUnroutable, match="selected 'claude'"):
        await runtime._initialize_handshake(runtime._harness.client_capabilities)
    assert send.await_count == 1
    assert not runtime._initialized
