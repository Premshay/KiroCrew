"""App session-end hook: registry semantics and the three firing sites."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_slot_close_recreation_race import NAME, _make_stale, _Req, _state_with_slot

from kiro_crew import autonudge
from kiro_crew.apps import teardown
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.subagent import SubagentInfo, SubagentManager

EVENT_KEYS = {"session_key", "provider", "provider_session_id", "reason", "ended_at"}


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(teardown, "_SESSION_END_HOOKS", {})
    monkeypatch.setattr(autonudge, "_INSTANCE", None)


def _recorder():
    events: list[dict] = []
    done = asyncio.Event()

    async def hook(event: dict) -> None:
        events.append(event)
        done.set()

    return hook, events, done


def _assert_shape(event: dict, *, key: str, provider: str, sid: str, reason: str) -> None:
    assert set(event) == EVENT_KEYS
    assert event["session_key"] == key
    assert event["provider"] == provider
    assert event["provider_session_id"] == sid
    assert event["reason"] == reason
    assert event["ended_at"].endswith("+00:00")


# ── registry ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_fans_out_to_every_app() -> None:
    a, a_events, _ = _recorder()
    b, b_events, _ = _recorder()
    teardown.register_session_end_hook("a", a)
    teardown.register_session_end_hook("b", b)

    await teardown.notify_session_ended({"session_key": "k"})

    assert a_events == b_events == [{"session_key": "k"}]


@pytest.mark.asyncio
async def test_reregister_replaces_and_unregister_drops() -> None:
    old, old_events, _ = _recorder()
    new, new_events, _ = _recorder()
    teardown.register_session_end_hook("a", old)
    teardown.register_session_end_hook("a", new)
    await teardown.notify_session_ended({"n": 1})
    assert old_events == [] and new_events == [{"n": 1}]

    teardown.unregister_session_end_hook("a")
    teardown.unregister_session_end_hook("a")  # safe when absent
    await teardown.notify_session_ended({"n": 2})
    assert new_events == [{"n": 1}]


@pytest.mark.asyncio
async def test_forget_app_hooks_clears_session_end_hook() -> None:
    hook, events, _ = _recorder()
    teardown.register_session_end_hook("a", hook)
    teardown.forget_app_hooks("a")
    await teardown.notify_session_ended({"n": 1})
    assert events == []


@pytest.mark.asyncio
async def test_failing_hook_is_isolated_and_logged(caplog) -> None:
    async def boom(event: dict) -> None:
        raise RuntimeError("nope")

    good, events, _ = _recorder()
    teardown.register_session_end_hook("bad", boom)
    teardown.register_session_end_hook("good", good)

    with caplog.at_level(logging.WARNING, logger=teardown.logger.name):
        await teardown.notify_session_ended({"session_key": "k"})

    assert events == [{"session_key": "k"}]
    assert any("session-end hook for app 'bad' failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_slow_hook_times_out_without_blocking_others(monkeypatch, caplog) -> None:
    monkeypatch.setattr(teardown, "SESSION_END_HOOK_TIMEOUT_S", 0.05)

    async def hang(event: dict) -> None:
        await asyncio.Event().wait()

    good, events, _ = _recorder()
    teardown.register_session_end_hook("slow", hang)
    teardown.register_session_end_hook("good", good)

    with caplog.at_level(logging.WARNING, logger=teardown.logger.name):
        await asyncio.wait_for(teardown.notify_session_ended({"session_key": "k"}), 2)

    assert events == [{"session_key": "k"}]
    assert any("timed out" in r.getMessage() for r in caplog.records)


def test_fire_without_running_loop_or_hooks_is_a_noop() -> None:
    teardown.fire_session_ended(session_key="k", reason="user_closed")  # no hooks
    teardown.register_session_end_hook("a", AsyncMock())
    teardown.fire_session_ended(session_key="k", reason="user_closed")  # no loop


# ── site (a): deliberate dismissal ──────────────────────────────────────────


def _map_with(state, key: str, provider: str, sid: str) -> None:
    state.sessions._session_map = MagicMock()
    state.sessions._session_map.get_provider.return_value = provider
    state.sessions._session_map.mapped_sid.return_value = sid


@pytest.mark.asyncio
async def test_user_close_fires_with_session_map_identity(tmp_path, monkeypatch) -> None:
    state = _state_with_slot(tmp_path)
    _map_with(state, f"dashboard:{NAME}", "claude_code", "sid-123")
    monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock())
    hook, events, done = _recorder()
    teardown.register_session_end_hook("a", hook)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    await asyncio.wait_for(done.wait(), 2)

    assert resp.status == 200
    assert len(events) == 1
    _assert_shape(
        events[0],
        key=f"dashboard:{NAME}",
        provider="claude_code",
        sid="sid-123",
        reason="user_closed",
    )


@pytest.mark.asyncio
async def test_failed_close_does_not_fire(tmp_path, monkeypatch) -> None:
    state = _state_with_slot(tmp_path)
    monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock(side_effect=OSError("disk")))
    hook, events, _ = _recorder()
    teardown.register_session_end_hook("a", hook)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    await asyncio.sleep(0)

    assert resp.status != 200
    assert events == []


# ── site (b): idle archival ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idle_archive_fires(tmp_path, monkeypatch) -> None:
    state = _state_with_slot(tmp_path)
    _make_stale(state)
    _map_with(state, f"dashboard:{NAME}", "acp", "")
    monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock())
    hook, events, done = _recorder()
    teardown.register_session_end_hook("a", hook)

    resp = await handlers.api_chat_slots_cleanup(_Req(state, NAME))
    await asyncio.wait_for(done.wait(), 2)

    assert resp.status == 200
    assert len(events) == 1
    _assert_shape(
        events[0], key=f"dashboard:{NAME}", provider="acp", sid="", reason="idle_archived"
    )


# ── site (c): subagent terminal ─────────────────────────────────────────────


async def _report(info: SubagentInfo, *, shutting_down: bool = False) -> None:
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    mgr._fire_event = AsyncMock()
    mgr._on_done = None
    mgr._shutting_down = shutting_down
    await mgr._report_terminal(
        info, source="test", injection_timeout_reason="x", mark_delivered_on_success=False
    )


def _info(**kw) -> SubagentInfo:
    info = SubagentInfo(id="a1", task="t", parent_session_key="dashboard:main")
    info._session_id = "acp-sid-9"
    info._session_provider = "codex"
    for k, v in kw.items():
        setattr(info, k, v)
    return info


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, "subagent_finished"),
        ({"error": "boom"}, "subagent_failed"),
        ({"user_stopped": True}, "subagent_cancelled"),
    ],
)
async def test_subagent_terminal_fires_with_outcome_reason(overrides, reason) -> None:
    hook, events, done = _recorder()
    teardown.register_session_end_hook("a", hook)

    await _report(_info(**overrides))
    await asyncio.wait_for(done.wait(), 2)

    assert len(events) == 1
    _assert_shape(events[0], key="subagent:a1", provider="codex", sid="acp-sid-9", reason=reason)


@pytest.mark.asyncio
async def test_continued_subagent_uses_conversation_key_and_empty_sid_when_unknown() -> None:
    hook, events, done = _recorder()
    teardown.register_session_end_hook("a", hook)

    await _report(_info(conversation_key="subagent:conv-1", _session_id="", _session_provider=""))
    await asyncio.wait_for(done.wait(), 2)

    _assert_shape(events[0], key="subagent:conv-1", provider="", sid="", reason="subagent_finished")


@pytest.mark.asyncio
async def test_subagent_terminal_during_gateway_shutdown_does_not_fire() -> None:
    hook, events, _ = _recorder()
    teardown.register_session_end_hook("a", hook)

    await _report(_info(), shutting_down=True)
    await asyncio.sleep(0)

    assert events == []
