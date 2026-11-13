"""Provider reset delegation, transport failure and disposable cleanup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_acp_client_new_conversation import _live_claude

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager


def _provider_over(client: object) -> AcpProvider:
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client  # type: ignore[assignment]
    return provider


@pytest.mark.asyncio
async def test_a_client_outside_the_set_has_no_reset_and_names_its_backend() -> None:
    provider = _provider_over(AcpClient(acp_backend=ACP_BACKEND_OPENCODE))

    with pytest.raises(NotImplementedError, match="'opencode' backend has no fresh-conversation"):
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
async def test_provider_dispatch_resets_a_live_claude_session(tmp_path, monkeypatch) -> None:
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    provider = _provider_over(client)
    provider.memory_mode = "persistent"
    provider._apply_initial_effort = AsyncMock()  # type: ignore[method-assign]

    await provider.new_conversation()

    assert client._session_id == "sess-2"
    assert "session/close sess-1" in steps
    assert "mcp array" in steps
    assert "model" in steps
    assert client.last_prompt_stats.context_pct == 0.0
    provider._apply_initial_effort.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_provider_dispatch_refuses_reset_during_a_turn(tmp_path, monkeypatch) -> None:
    steps: list[str] = []
    client = _live_claude(tmp_path, monkeypatch, steps)
    client.has_active_turn = lambda: True  # type: ignore[method-assign]
    provider = _provider_over(client)
    provider.memory_mode = "persistent"

    with pytest.raises(AcpError, match="turn is in flight"):
        await provider.new_conversation()

    assert client._session_id == "sess-1"
    assert steps == []


@pytest.mark.asyncio
async def test_late_provider_mapping_does_not_restore_a_retracted_cwd() -> None:
    manager = SessionManager.__new__(SessionManager)
    provider = SimpleNamespace(session_id="new-sid", session_provider_label="claude", cwd="/old")
    manager._sessions = {
        "dashboard:test": SimpleNamespace(
            provider=provider, retire_on_identity_change=False, cwd_retracted=False
        )
    }
    manager._fold_key = lambda key: key  # type: ignore[method-assign]
    manager._session_map = MagicMock()
    manager._allocation_boundary = MagicMock()  # type: ignore[method-assign]

    manager.retract_cwd("dashboard:test")

    await manager.persist_provider_session("dashboard:test", provider)

    manager._session_map.set.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_mode", "left_on", "deleted"),
    [
        pytest.param("persistent", "sess-2", [], id="persistent-kept"),
        pytest.param("incognito", "sess-2", ["sess-1"], id="disposable-reset"),
        pytest.param("incognito", None, ["sess-1"], id="disposable-failed-reset"),
        pytest.param("incognito", "sess-1", [], id="disposable-refused-before-reset"),
    ],
)
async def test_a_claude_reset_deletes_a_disposable_transcript_once_it_left_it(
    tmp_path, memory_mode, left_on, deleted
) -> None:
    """A disposable session's replaced transcript goes, as ``shutdown`` would delete it.

    A failed reset ends the client's process and clears its id, so nothing else would
    name the old session; one refused before it touched the session leaves the id for
    the caller's own ``shutdown``. The effort ``start`` applies follows a reset only.
    """
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE, work_dir=tmp_path)
    client._session_id = "sess-1"

    async def reset() -> None:
        client._session_id = left_on
        if left_on != "sess-2":
            raise AcpError("reset failed")

    client.new_conversation = reset  # type: ignore[method-assign]
    provider = _provider_over(client)
    provider.memory_mode = memory_mode
    provider._apply_initial_effort = AsyncMock()  # type: ignore[method-assign]
    removed: list[str] = []
    provider.cleanup_session = AsyncMock(side_effect=removed.append)  # type: ignore[method-assign]

    if left_on == "sess-2":
        await provider.new_conversation()
        provider._apply_initial_effort.assert_awaited_once_with()
    else:
        with pytest.raises(AcpError):
            await provider.new_conversation()
        provider._apply_initial_effort.assert_not_awaited()
    assert removed == deleted
