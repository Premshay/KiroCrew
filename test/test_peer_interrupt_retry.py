"""A peer interrupt claude held while a tool ran is retried at the next tool boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.channel import ChannelMessage
from kiro_crew.dashboard.handlers_channel import deliver_attached_channel_message
from kiro_crew.dashboard.peer_interrupt_retry import (
    held_interrupts,
    retry_after_tool_result,
    retry_held_peer_interrupts,
)
from kiro_crew.dashboard.state import PEER_CHANNEL_REQUEST_KIND, DashboardState
from kiro_crew.history import ConversationLog


def _state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid.return_value = None
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


class _Channel:
    def __init__(self, message: ChannelMessage) -> None:
        self.id = "deadbeef"
        self._msg_index = {message.id: message}
        self.saved = 0

    def _save(self) -> None:
        self.saved += 1


async def _held(monkeypatch, tmp_path, steer_results):
    """A running turn whose steer is first held, then answers ``steer_results``."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot("crew-claude")
    slot.task = MagicMock()
    slot.task.done.return_value = False
    steer = AsyncMock(side_effect=[False, *steer_results])
    slot._acp_client = SimpleNamespace(supports_steer=True, steer=steer)
    slot.queue_append("later")
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    message = ChannelMessage(
        id="interrupt9",
        from_id="peer",
        from_role="Baton",
        mention=["claude"],
        msg_type="mention",
        delivery="interrupt",
        content="Hold the push.",
    )
    channel = _Channel(message)
    state.channel_manager = SimpleNamespace(get=lambda cid: channel if cid == channel.id else None)
    member = SimpleNamespace(id="m7", session_key="dashboard:crew-claude")
    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", AsyncMock())
    # The slots broadcast reads the real channel manager; this fake stands in for it.
    monkeypatch.setattr(state, "push_slots_update", MagicMock())
    outcome = await deliver_attached_channel_message(state, channel, member, message)
    return state, slot, steer, channel, message, outcome


@pytest.mark.asyncio
async def test_a_held_interrupt_steers_at_the_next_tool_boundary(monkeypatch, tmp_path) -> None:
    state, slot, steer, channel, message, outcome = await _held(monkeypatch, tmp_path, [True])
    assert outcome == "held"
    assert len(held_interrupts(slot)) == 1
    assert slot._queue[0]["kind"] == PEER_CHANNEL_REQUEST_KIND

    delivered = await retry_held_peer_interrupts(state, slot)

    assert delivered == 1
    assert steer.await_count == 2
    # The queued copy is gone, so the interrupt does not run a second time.
    assert [item["content"] for item in slot._queue] == ["later"]
    assert held_interrupts(slot) == []
    assert slot.messages[-1]["role"] == "inject"
    assert "Hold the push." in slot.messages[-1]["content"]
    assert message.delivery_status["m7"] == "steered" and channel.saved == 1


@pytest.mark.asyncio
async def test_a_still_held_interrupt_waits_for_the_next_boundary(monkeypatch, tmp_path) -> None:
    state, slot, steer, _channel, _message, _ = await _held(monkeypatch, tmp_path, [False, True])

    assert await retry_held_peer_interrupts(state, slot) == 0
    assert len(held_interrupts(slot)) == 1
    assert slot._queue[0]["kind"] == PEER_CHANNEL_REQUEST_KIND
    assert slot._pending_steers == []

    assert await retry_held_peer_interrupts(state, slot) == 1
    assert steer.await_count == 3


@pytest.mark.asyncio
async def test_an_interrupt_the_queue_already_ran_is_not_steered(monkeypatch, tmp_path) -> None:
    state, slot, steer, _channel, _message, _ = await _held(monkeypatch, tmp_path, [True])
    # The turn ended and the queue ran the held copy before any tool boundary.
    slot.queue_remove_by_id(slot._queue[0]["id"])

    assert await retry_held_peer_interrupts(state, slot) == 0
    assert steer.await_count == 1
    assert held_interrupts(slot) == []


@pytest.mark.asyncio
async def test_no_retry_once_the_turn_is_over(monkeypatch, tmp_path) -> None:
    state, slot, steer, _channel, _message, _ = await _held(monkeypatch, tmp_path, [True])
    slot.task.done.return_value = True

    assert await retry_held_peer_interrupts(state, slot) == 0
    assert steer.await_count == 1


@pytest.mark.asyncio
async def test_the_turn_loop_hook_never_raises(monkeypatch, tmp_path) -> None:
    state, slot, steer, _channel, _message, _ = await _held(monkeypatch, tmp_path, [])
    steer.side_effect = RuntimeError("boom")

    await retry_after_tool_result(state, slot)


def test_the_turn_loop_retries_after_every_tool_result() -> None:
    import inspect

    from kiro_crew.dashboard import chat_runner

    source = inspect.getsource(chat_runner)
    i = source.index("elif event.kind == EVENT_TOOL_RESULT:")
    assert "retry_after_tool_result(turn.state, turn.slot)" in source[i : i + 300]
