"""Edition transport templates select their seat only through an explicit opt-in."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_agent_selection_helpers import _finished, _template_chat
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in", [True, False, "true"])
@pytest.mark.parametrize("refused", [False, True])
@pytest.mark.parametrize("mismatched", [False, True])
async def test_engine_template_selection_is_atomic_and_opt_in(
    tmp_path, monkeypatch, opt_in, refused, mismatched
):
    state, slot, _ = await _finished(
        _template_chat(tmp_path, monkeypatch, first_turn=False), "template setup"
    )
    target = "transport-template"
    cfg = KiroCrewConfig.load()
    cfg.agents[target] = KiroCrewAgentConfig(kiro_agent=target)
    cfg.save()
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name == target else "",
    )
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(
            providers=SimpleNamespace(
                agent_runtime_policy=lambda _name: {"template_selects_runtime": opt_in}
            )
        ),
    )
    slot.runtime_agent = "old-seat"
    slot.model = "old-model"
    slot.reasoning_effort = "high"
    slot.served_model = "old-live-model"
    twin = state.get_or_create_slot("linked-view")
    twin.linked_session_key = "dashboard:template-chat"
    twin.agent = "different-member" if mismatched else slot.agent
    twin.memory_store = slot.memory_store
    twin.runtime_agent = "old-seat"
    twin.model = "old-model"
    twin.reasoning_effort = "high"
    twin.served_model = "old-live-model"
    twin._active_fallback_model = "old-fallback"
    twin._fallback_walked = ["old-fallback"]
    state.sessions.reset = AsyncMock(return_value=True)
    if refused:
        monkeypatch.setattr(
            state.conversation_log, "_locked", MagicMock(side_effect=TimeoutError("history lock"))
        )
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post(
                f"/api/chat/slots/{slot.key}/agent",
                json={"agent": target, "agent_kind": "template"},
            ),
            "engine template selection",
        )
        blocked = opt_in is True and mismatched
        assert response.status == (
            409 if blocked else 503 if refused else 200
        ), await response.text()
    if opt_in is True and not refused and not mismatched:
        for view in (slot, twin):
            assert view.runtime_agent == target
            assert (view.model, view.reasoning_effort, view.served_model) == ("", "", "")
        metadata = state.conversation_log.get_metadata("dashboard:template-chat")
        assert metadata["runtime_agent"] == target
        assert metadata["reasoning_effort"] == ""
        assert twin._active_fallback_model == ""
        assert twin._fallback_walked == []
        state.sessions._session_map.mark_seat_switch.assert_called_once_with(
            "dashboard:template-chat"
        )
    else:
        for view in (slot, twin):
            assert view.runtime_agent == "old-seat"
            assert view.reasoning_effort == "high"
        assert twin.served_model == "old-live-model"
        assert twin._active_fallback_model == "old-fallback"
        assert twin._fallback_walked == ["old-fallback"]
        if refused or blocked:
            assert slot.model == "old-model"
            assert twin.model == "old-model"
