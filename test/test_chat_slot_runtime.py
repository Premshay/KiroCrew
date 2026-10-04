import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_persistence import _restore_model_fields
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


@pytest.fixture
def runtime_state(monkeypatch):
    slot = _ChatSlot("member-runtime")
    slot.agent = "vernier"
    slot.mode = "member"
    slot.memory_store = "member-private"
    slot.model = "original-model"
    slot.reasoning_effort = "high"
    slot.project = "original-project"
    slot.messages = [{"role": "user", "text": "remember this"}]
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider.return_value = None
    cfg = KiroCrewConfig.load()
    cfg.agents = {
        "vernier": KiroCrewAgentConfig(kiro_agent="saved-member", member_id="vernier"),
        "claude": KiroCrewAgentConfig(kiro_agent="claude-engine"),
        "codex": KiroCrewAgentConfig(kiro_agent="codex-engine"),
        "deepseek": KiroCrewAgentConfig(kiro_agent="deepseek-engine"),
        "antigravity": KiroCrewAgentConfig(kiro_agent="antigravity-engine"),
    }
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(
            providers=SimpleNamespace(
                agent_runtime_policy=lambda name: {
                    "engine": name,
                    "backend": (
                        "claude"
                        if name == "claude-engine"
                        else "codex"
                        if name == "codex-engine"
                        else "deepseek"
                        if name == "deepseek-engine"
                        else "antigravity_headless"
                    ),
                    "priority": 0 if name == "claude-engine" else 1,
                }
            )
        ),
    )
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", reset)
    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", AsyncMock(return_value=None))
    return state, slot, reset


def runtime_app(state):
    app = as_owner(web.Application())
    app["state"] = state
    app.router.add_route(
        "GET", "/api/chat/slots/{slot}/runtime", chat_handlers.api_chat_slot_runtime
    )
    app.router.add_route(
        "POST", "/api/chat/slots/{slot}/runtime", chat_handlers.api_chat_slot_runtime
    )
    return app


@pytest.mark.asyncio
async def test_runtime_switch_preserves_member_store_history_and_project(runtime_state):
    state, slot, reset = runtime_state
    before = (
        slot.agent,
        slot.memory_store,
        slot.messages[:],
        slot.project,
        effective_session_key(slot),
    )
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 200, await response.text()
    assert (
        slot.agent,
        slot.memory_store,
        slot.messages,
        slot.project,
        effective_session_key(slot),
    ) == before
    assert slot.runtime_agent == "claude"
    assert (slot.model, slot.reasoning_effort) == ("", "")
    assert slot._dirty
    reset.assert_awaited_once()
    state.sessions._session_map.mark_seat_switch.assert_called_once_with(before[4])


@pytest.mark.asyncio
async def test_refused_switch_leaves_no_seat_switch_mark(runtime_state):
    state, slot, reset = runtime_state
    reset.side_effect = RuntimeError("pre-pop reset failure")
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 500
    state.sessions._session_map.mark_seat_switch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["unknown", "antigravity"])
async def test_unverified_member_runtime_is_refused_without_mutation(runtime_state, selected):
    # antigravity is here deliberately: it has no way to confirm a member's saved
    # spec, so it must be refused the same way an unknown backend is -- not offered
    # and then killed on the first prompt with capability_runtime_unverified.
    state, slot, reset = runtime_state
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": selected}
        )
        assert response.status == 409
    assert slot.runtime_agent == ""
    assert slot.model == "original-model"
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_seat_declaring_member_capabilities_is_offered_and_accepted(
    runtime_state, monkeypatch
):
    """A companion seat outside the ACP backends may declare member support; it is
    then held to loaded_stamp at allocation like any other member backend."""
    state, slot, reset = runtime_state
    from kiro_crew.platform import context as platform_context

    base_policy = platform_context.current_context().providers.agent_runtime_policy

    def policy(name):
        row = base_policy(name)
        if name == "antigravity-engine":
            row["member_capabilities"] = True
        return row

    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(providers=SimpleNamespace(agent_runtime_policy=policy)),
    )
    async with TestClient(TestServer(runtime_app(state))) as client:
        listed = await (await client.get(f"/api/chat/slots/{slot.key}/runtime")).json()
        row = next(c for c in listed["choices"] if c["name"] == "antigravity")
        assert (row["supported"], row["member_capable"]) == (True, True)
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "antigravity"}
        )
        assert response.status == 200, await response.text()
    assert slot.runtime_agent == "antigravity"
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_busy_member_refuses_switch(runtime_state):
    state, slot, reset = runtime_state
    slot.task = MagicMock()
    slot.task.done.return_value = False
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 409
    assert slot.runtime_agent == ""
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_failure_rolls_back_selection(runtime_state):
    """A pre-pop raise leaves the old session alive on the old bindings, so the
    answer is a 500 and the committed selection rolls back. A False verdict is NOT
    a failure -- see the declined-reset tests below: it means nothing was torn
    down, and the next message cold-starts on the new seat."""
    state, slot, reset = runtime_state
    reset.side_effect = RuntimeError("pre-pop reset failure")
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 500
    assert (slot.runtime_agent, slot.model, slot.reasoning_effort) == ("", "original-model", "high")


@pytest.mark.asyncio
async def test_declined_reset_with_no_live_session_still_switches(runtime_state):
    """A False reset verdict with no registered provider means there was nothing to
    tear down, not a busy decline: the next message cold-starts on the selected
    seat. Treating it as a 409 is what made every switch on a fresh conversation
    answer 'session changed during execution switch'."""
    state, slot, reset = runtime_state
    reset.return_value = False
    state.sessions.get_provider.return_value = None
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 200, await response.text()
    assert slot.runtime_agent == "claude"
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_declined_reset_with_idle_live_session_retries_then_refuses(runtime_state):
    """A live provider that is idle declined for a non-busy reason; tearing down an
    idle session is safe, so the handler retries once. A second decline is a turn
    genuinely racing the switch, answered with the same 409 the fast path gives."""
    from kiro_crew.providers.base import LLMProvider

    state, slot, reset = runtime_state
    live = MagicMock(spec=LLMProvider)
    live.has_active_turn.return_value = False
    state.sessions.get_provider.return_value = live
    reset.return_value = False
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 409, await response.text()
        assert (await response.json())["code"] == "turn_in_flight"
    assert reset.await_count == 2
    assert slot.runtime_agent == ""


@pytest.mark.asyncio
async def test_choices_show_projection_gap(runtime_state):
    state, slot, _ = runtime_state
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.get(f"/api/chat/slots/{slot.key}/runtime")
        choices = (await response.json())["choices"]
    assert [choice["name"] for choice in choices] == ["claude", "codex", "deepseek", "antigravity"]
    assert [choice["supported"] for choice in choices] == [True, True, True, False]


@pytest.mark.asyncio
async def test_linked_views_receive_same_selection(runtime_state):
    state, slot, _ = runtime_state
    twin = _ChatSlot("linked-view")
    twin.agent = slot.agent
    twin.memory_store = slot.memory_store
    twin.linked_session_key = effective_session_key(slot)
    state._slots[twin.key] = twin
    assert effective_session_key(twin) == effective_session_key(slot)
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 200, await response.text()
    assert twin.runtime_agent == slot.runtime_agent == "claude"
    assert twin._dirty


def test_restore_execution_choice_keeps_target_runtime_default(runtime_state):
    _, slot, _ = runtime_state
    restored = _ChatSlot(slot.key)
    cfg = KiroCrewConfig.load()
    assert _restore_model_fields(restored, {"runtime_agent": "codex", "model": ""}, cfg=cfg)
    assert restored.runtime_agent == "codex"
    assert restored.model == ""
    assert not _restore_model_fields(restored, {"runtime_agent": "vernier"}, cfg=cfg)
    assert restored.runtime_agent == ""
    assert not _restore_model_fields(restored, {"runtime_agent": "removed-seat"}, cfg=cfg)


@pytest.mark.asyncio
async def test_cold_model_controls_use_execution_backend(runtime_state):
    _, slot, _ = runtime_state
    slot.runtime_agent = "codex"
    assert await chat_handlers._configured_backend_for_slot(slot) == "codex"
    slot.runtime_agent = "deepseek"
    assert await chat_handlers._configured_backend_for_slot(slot) == "deepseek"


@pytest.mark.asyncio
async def test_same_runtime_does_not_clear_current_model(runtime_state):
    state, slot, reset = runtime_state
    slot.runtime_agent = "claude"
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 200
        assert (await response.json())["model"] == "original-model"
    reset.assert_not_awaited()
    assert slot.model == "original-model"


@pytest.mark.asyncio
async def test_app_token_cannot_switch_runtime(runtime_state):
    state, slot, reset = runtime_state
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime",
            json={"runtime_agent": "claude"},
            headers={"X-Test-App": "plugin"},
        )
        assert response.status == 403
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_children_prevent_runtime_switch(runtime_state, monkeypatch):
    state, slot, reset = runtime_state
    monkeypatch.setattr(
        chat_handlers,
        "_subagents_attached_response",
        AsyncMock(return_value=web.json_response({"error": "children active"}, status=409)),
    )
    async with TestClient(TestServer(runtime_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{slot.key}/runtime", json={"runtime_agent": "claude"}
        )
        assert response.status == 409
    reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("popped", [False, True])
async def test_cancellation_keeps_selection_consistent_with_session(
    runtime_state, monkeypatch, popped
):
    from dashboard_owner_helpers import owner_claims

    state, slot, reset = runtime_state
    prior = object()
    state.sessions.get_provider.return_value = prior
    slot._active_fallback_model = "old-fallback"

    async def cancelled(*args, **kwargs):
        assert slot._active_fallback_model == ""
        if popped:
            state.sessions.get_provider.return_value = None
        raise asyncio.CancelledError()

    reset.side_effect = cancelled
    monkeypatch.setattr(
        chat_handlers,
        "read_bounded_json",
        AsyncMock(return_value=({"runtime_agent": "claude"}, None)),
    )
    request = owner_claims(MagicMock(spec=web.Request))
    request.app = {"state": state}
    request.match_info = {"slot": slot.key}
    with pytest.raises(asyncio.CancelledError):
        await chat_handlers.api_chat_slot_runtime(request)
    assert slot.runtime_agent == ("claude" if popped else "")
    assert slot._active_fallback_model == ("" if popped else "old-fallback")
