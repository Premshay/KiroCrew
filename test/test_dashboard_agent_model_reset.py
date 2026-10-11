"""An explicit agent pick inherits the target's model and rolls back atomically."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from test_chat_slot_switch_atomicity import _make_app, _mock_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, ResolvedBindings
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMProvider
from kiro_crew.testing.wait import until_parked

MODEL_STATE = {
    "model": "writer-model",
    "_model_pick_gen": 7,
    "_fallback_candidate_idx": 2,
    "_fallback_walked": ["fallback-model"],
    "_active_fallback_model": "fallback-model",
    "_fallback_primary_model": "writer-model",
    "_fallback_slot_model": "writer-model",
    "_fallback_pick_gen": 7,
    "_refusal_fallback_primary": "writer-model",
    "_refusal_fallback_candidate": "refusal-model",
}


def model_state(slot):
    return {field: getattr(slot, field) for field in MODEL_STATE}


@pytest.fixture
def agent_switch(monkeypatch, tmp_path):
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", model="writer-model")
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", model="reviewer-model")
    monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(chat_handlers, "warm_project_agent_names", AsyncMock())
    monkeypatch.setattr(
        chat_handlers,
        "resolve_agent_bindings",
        lambda config, name, *args, **kwargs: ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="default",
            effective_memory_config={},
            kiro_agent="kirocrew",
            model=config.agents[name].model,
            selection_kind=kwargs.get("selection_kind") or "member",
        ),
    )
    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", AsyncMock())
    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    slot = _ChatSlot("test")
    slot.agent = "writer"
    slot.agent_kind = "member"
    for field, value in MODEL_STATE.items():
        setattr(slot, field, list(value) if isinstance(value, list) else value)
    state = _mock_state(slot)
    state.sessions.reset.return_value = True
    state.conversation_log = ConversationLog()
    state.conversation_log.append("dashboard:test", "user", "hello", agent=slot.agent)
    state.conversation_log.update_metadata("dashboard:test", {"model": slot.model})
    return cfg, state, slot


@pytest.mark.asyncio
async def test_same_harness_agent_switch_inherits_target_model(agent_switch):
    cfg, state, slot = agent_switch
    assert cfg.agents["writer"].kiro_agent == cfg.agents["reviewer"].kiro_agent
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        response = await client.post("/api/chat/slots/test/agent", json={"agent": "reviewer"})
        assert response.status == 200
        data = await response.json()
    assert slot.model == ""
    assert data["model"] == ""
    assert chat_runner._default_session_model(cfg, slot, "") == "reviewer-model"
    assert slot._model_pick_gen == 8
    assert slot._fallback_candidate_idx == slot._fallback_pick_gen == 0
    assert slot._fallback_walked == []
    assert slot._active_fallback_model == slot._fallback_primary_model == ""
    assert slot._fallback_slot_model == slot._refusal_fallback_primary == ""
    assert slot._refusal_fallback_candidate == ""
    metadata = state.conversation_log.get_metadata("dashboard:test")
    assert metadata["model"] == ""


@pytest.mark.asyncio
async def test_same_agent_reset_preserves_model_state(agent_switch):
    _, state, slot = agent_switch
    before = model_state(slot)
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        response = await client.post("/api/chat/slots/test/agent", json={"agent": "writer"})
        assert response.status == 200
        assert (await response.json())["model"] == "writer-model"
    assert model_state(slot) == before
    state.sessions.reset.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_kind", [False, True])
async def test_same_name_resolved_namespace_change_clears_model(
    agent_switch, monkeypatch, explicit_kind
):
    _, state, slot = agent_switch
    resolve = chat_handlers.resolve_agent_bindings

    def resolve_template(*args, **kwargs):
        kwargs["selection_kind"] = "template"
        return resolve(*args, **kwargs)

    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve_template)
    body = {"agent": "writer"}
    if explicit_kind:
        body["agent_kind"] = "template"
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        response = await client.post("/api/chat/slots/test/agent", json=body)
        assert response.status == 200
    assert slot.agent_kind == "template"
    assert slot.model == slot._active_fallback_model == ""
    assert slot._model_pick_gen == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pre_pop", "rebound", "history"])
async def test_failed_agent_switch_restores_model_state(agent_switch, monkeypatch, failure):
    _, state, slot = agent_switch
    before = model_state(slot)
    expected_status = {"pre_pop": 500, "rebound": 409, "history": 503}[failure]
    if failure == "pre_pop":
        provider = MagicMock(spec=LLMProvider)
        provider.has_active_turn.return_value = False
        state.sessions.get_provider.return_value = provider
        state.sessions.reset.side_effect = RuntimeError("pre-pop failure")
    elif failure == "rebound":

        async def reset(*args, **kwargs):
            assert slot.model == slot._active_fallback_model == ""
            slot.linked_session_key = "cron:new-binding"
            return True

        state.sessions.reset.side_effect = reset
    else:
        update = state.conversation_log.update_metadata
        writes = 0

        def fail_first_write(*args, **kwargs):
            nonlocal writes
            writes += 1
            update(*args, **kwargs)
            if writes == 1:
                raise OSError("history unavailable")

        monkeypatch.setattr(state.conversation_log, "update_metadata", fail_first_write)
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        response = await client.post("/api/chat/slots/test/agent", json={"agent": "reviewer"})
        assert response.status == expected_status
    assert slot.agent == "writer"
    assert model_state(slot) == before
    assert state.conversation_log.get_metadata("dashboard:test")["model"] == "writer-model"
    assert slot._dirty


@pytest.mark.asyncio
@pytest.mark.parametrize("new_model", ["", "newer-model"])
@pytest.mark.parametrize("new_agent_write", [False, True])
async def test_failed_switch_spares_newer_model_or_agent_pick(
    agent_switch, new_model, new_agent_write
):
    _, state, slot = agent_switch
    after_pick = {}

    async def reset(*args, **kwargs):
        if new_agent_write:
            slot.agent = str(slot.agent)
            slot.agent_kind = str(slot.agent_kind)
        slot.model = new_model
        slot._model_pick_gen += 1
        slot._active_fallback_model = "newer-fallback"
        after_pick.update(model_state(slot))
        slot.linked_session_key = "cron:new-binding"
        return True

    state.sessions.reset.side_effect = reset
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        response = await client.post("/api/chat/slots/test/agent", json={"agent": "reviewer"})
        assert response.status == 409
    assert slot.agent == ("reviewer" if new_agent_write else "writer")
    assert model_state(slot) == after_pick


@pytest.mark.asyncio
async def test_agent_switch_waits_for_model_pick_lock(agent_switch):
    _, state, slot = agent_switch
    task = None
    try:
        async with slot._model_pick_lock:
            task = asyncio.create_task(
                chat_handlers.switch_slot_agent(
                    state,
                    slot,
                    slot.key,
                    "reviewer",
                    "",
                    chat_handlers.SlotAgentSwitchCaller(app="", remote="", owner=True),
                )
            )
            await until_parked(slot._model_pick_lock, timeout=5)
            assert model_state(slot) == MODEL_STATE
            state.sessions.reset.assert_not_awaited()
        response = await asyncio.wait_for(task, timeout=5)
        assert response.status == 200
        assert slot.model == ""
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["agent", "model"])
async def test_queued_model_pick_and_agent_switch_rollback_independently(
    agent_switch, monkeypatch, failure
):
    _, state, slot = agent_switch
    entered, release = asyncio.Event(), asyncio.Event()
    provider = MagicMock(spec=LLMProvider)
    provider.has_active_turn.return_value = False
    state.sessions.get_provider.return_value = provider
    calls = 0

    async def reset(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
        if calls == 1 and failure == "agent":
            raise RuntimeError("pre-pop failure")
        return True

    state.sessions.reset.side_effect = reset

    async def live_switch(*args, **kwargs):
        if failure == "model":
            raise chat_handlers.AcpModelUnavailable("auto", backend="kiro")
        return False

    monkeypatch.setattr(chat_handlers, "_try_live_model_switch", live_switch)
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        agent_task = asyncio.create_task(
            client.post("/api/chat/slots/test/agent", json={"agent": "reviewer"})
        )
        tasks = [agent_task]
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            model_task = asyncio.create_task(
                client.post("/api/chat/slots/test/model", json={"model": "auto"})
            )
            tasks.append(model_task)
            await until_parked(slot._lock, timeout=5)
            assert slot._model_pick_lock.locked()
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert [response.status for response in responses] == (
                [500, 200] if failure == "agent" else [200, 400]
            )
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
    assert slot.agent == ("writer" if failure == "agent" else "reviewer")
    assert slot.model == ("auto" if failure == "agent" else "")
    assert slot._model_pick_gen == 8
