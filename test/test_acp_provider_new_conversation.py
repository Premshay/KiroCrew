"""Provider reset delegation and the fork's warm ACP conversation contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.providers.acp import AcpProvider


def _provider_over(client: object) -> AcpProvider:
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client  # type: ignore[assignment]
    return provider


@pytest.mark.asyncio
async def test_a_client_without_reset_names_its_backend() -> None:
    provider = _provider_over(MagicMock(spec=["backend"], backend=ACP_BACKEND_CLAUDE))

    with pytest.raises(NotImplementedError, match="'claude' backend has no fresh-conversation"):
        await provider.new_conversation()


@pytest.mark.asyncio
async def test_a_client_with_a_reset_is_delegated_to() -> None:
    client = MagicMock(spec=["new_conversation", "backend"])
    client.new_conversation = AsyncMock()
    provider = _provider_over(client)

    await provider.new_conversation()

    client.new_conversation.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_a_dead_claude_client_reports_process_death() -> None:
    from kiro_crew.acp.transport_errors import AcpProcessDied

    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    with pytest.raises(AcpProcessDied):
        await _provider_over(client).new_conversation()


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_fails", [False, True])
async def test_warm_claude_reset_reapplies_startup_and_preserves_old_id_on_failure(
    monkeypatch: pytest.MonkeyPatch, startup_fails: bool
) -> None:
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = "prior"
    monkeypatch.setattr(client, "is_process_alive", lambda: True)
    monkeypatch.setattr(client, "has_active_turn", lambda: False)
    opened = AsyncMock(return_value={"sessionId": "fresh"})
    monkeypatch.setattr(client, "_new_session_following_substitution", opened)
    monkeypatch.setattr(client, "_capture_available_models", MagicMock())
    monkeypatch.setattr(client, "_store_session_config", MagicMock())
    startup = AsyncMock(side_effect=RuntimeError("model refused") if startup_fails else None)
    monkeypatch.setattr(client, "_apply_startup_model", startup)

    if startup_fails:
        with pytest.raises(RuntimeError, match="model refused"):
            await _provider_over(client).new_conversation()
        assert client._session_id == "prior"
    else:
        await _provider_over(client).new_conversation()
        assert client._session_id == "fresh"
    opened.assert_awaited_once_with()
    startup.assert_awaited_once_with()
