"""A peer interrupt claude held while a tool ran is retried at the next tool boundary."""

from __future__ import annotations

import asyncio
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
from kiro_crew.testing.wait import async_wait_until, await_until


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
async def test_turn_ending_during_retry_keeps_only_the_existing_fallback(monkeypatch, tmp_path):
    from kiro_crew.dashboard.chat_turn.steer_queue import _requeue_unconsumed_steers

    state, slot, steer, _channel, _message, _ = await _held(monkeypatch, tmp_path, [])
    original_ids = [entry["id"] for entry in slot._queue]

    async def turn_ends(text):
        _requeue_unconsumed_steers(state, slot)
        slot.task.done.return_value = True
        return False

    steer.side_effect = turn_ends
    assert await retry_held_peer_interrupts(state, slot) == 0
    assert [entry["id"] for entry in slot._queue] == original_ids
    assert slot._pending_steers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["turn", "client", "ended", "cancelled"])
async def test_late_acceptance_does_not_recreate_fallback(monkeypatch, tmp_path, change):
    from kiro_crew.dashboard.chat_turn.steer_queue import _requeue_unconsumed_steers

    state, slot, steer, _channel, message, _ = await _held(monkeypatch, tmp_path, [])
    held = held_interrupts(slot)[0]
    message.delivery_status["m7"] = "held"

    async def accept_after_ownership_changes(text):
        if change == "turn":
            slot.task = MagicMock()
        elif change == "client":
            slot._acp_client = MagicMock()
        elif change == "ended":
            slot.task.done.return_value = True
        else:
            slot.queue_remove_by_id(held.queue_id)
        return True

    steer.side_effect = accept_after_ownership_changes
    assert await retry_held_peer_interrupts(state, slot) == 0
    assert slot._pending_steers == []
    assert message.delivery_status["m7"] == "held"
    assert not [row for row in slot.messages if row["role"] == "inject"]
    queue_ids = [entry["id"] for entry in slot._queue]
    _requeue_unconsumed_steers(state, slot)
    assert [entry["id"] for entry in slot._queue] == queue_ids


@pytest.mark.asyncio
async def test_the_turn_loop_hook_never_raises(monkeypatch, tmp_path) -> None:
    state, slot, steer, _channel, _message, _ = await _held(monkeypatch, tmp_path, [])
    steer.side_effect = RuntimeError("boom")

    await retry_after_tool_result(state, slot)
    await await_until(slot._peer_interrupt_retry_task, what="failed retry cleanup")


@pytest.mark.asyncio
async def test_tool_boundary_keeps_reading_while_one_retry_awaits_its_answer(monkeypatch, tmp_path):
    state, slot, steer, channel, message, _ = await _held(monkeypatch, tmp_path, [])
    sent = asyncio.Event()
    answer = asyncio.Event()

    async def accepted_by_reader(text):
        sent.set()
        await await_until(answer, what="steering acknowledgement read by turn")
        return True

    steer.side_effect = accepted_by_reader
    retry = None
    try:
        # The event consumer must return here before it can read the answer.
        await await_until(
            asyncio.ensure_future(retry_after_tool_result(state, slot)),
            what="tool-result handler return",
        )
        await await_until(sent, what="one steering request")
        retry = slot._peer_interrupt_retry_task
        for _ in range(6):
            await retry_after_tool_result(state, slot)
        assert slot._peer_interrupt_retry_task is retry
        assert steer.await_count == 2  # initial hold plus one live request
        answer.set()
        await await_until(retry, what="accepted retry cleanup")
        await retry_after_tool_result(state, slot)
        assert steer.await_count == 2
        assert held_interrupts(slot) == []
        assert [item["content"] for item in slot._queue] == ["later"]
        assert slot.peer_channel_inbox_payload() == []
        assert message.delivery_status["m7"] == "steered"
        assert len([row for row in slot.messages if row["role"] == "inject"]) == 1
    finally:
        answer.set()
        if retry is not None:
            await await_until(retry, what="retry teardown")


def test_the_turn_loop_retries_after_every_tool_result() -> None:
    import inspect

    from kiro_crew.dashboard import chat_runner

    source = inspect.getsource(chat_runner)
    i = source.index("elif event.kind == EVENT_TOOL_RESULT:")
    assert "retry_after_tool_result(turn.state, turn.slot)" in source[i : i + 300]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["task", "_acp_client"])
async def test_scheduled_retry_does_not_cross_turn_or_client_replacement(
    monkeypatch, tmp_path, replacement
):
    state, slot, steer, _channel, _message, _ = await _held(monkeypatch, tmp_path, [True])
    await retry_after_tool_result(state, slot)
    retry = slot._peer_interrupt_retry_task
    setattr(slot, replacement, MagicMock())
    await await_until(retry, what="superseded retry")
    assert steer.await_count == 1
    assert len(held_interrupts(slot)) == 1
    assert slot._queue[0]["kind"] == PEER_CHANNEL_REQUEST_KIND


@pytest.mark.asyncio
async def test_real_acp_reader_acknowledges_retry_once(monkeypatch, tmp_path):
    from test_claude_steering_request import _client, _close_tool, _open_tool, _spawn, _Tasks, _Wire

    from kiro_crew.acp.types import EVENT_STEER_CONSUMED, EVENT_TOOL_RESULT

    state, slot, _steer, _channel, message, _ = await _held(monkeypatch, tmp_path, [])
    client = _client()
    wire = _Wire(client)
    slot._acp_client = client
    handled = asyncio.Event()

    async def consume():
        events = []
        async for event in client.stream_events("work"):
            events.append(event)
            if event.kind == EVENT_TOOL_RESULT:
                await retry_after_tool_result(state, slot)
                handled.set()
        return events

    async with _Tasks() as spawned:
        turn = _spawn(spawned, consume())
        slot.task = turn
        await async_wait_until(lambda: client._steering_turn_req is not None)
        wire.inbound.put_nowait(_open_tool())
        wire.inbound.put_nowait(_close_tool())
        request_id = await wire.next_steering_id()
        wire.answer(request_id, "injected")
        await await_until(handled, what="tool handler releases ACP reader")
        await async_wait_until(lambda: message.delivery_status.get("m7") == "steered")
        for number in range(6):
            wire.inbound.put_nowait(_open_tool(f"later-{number}"))
            wire.inbound.put_nowait(_close_tool(f"later-{number}"))
        wire.end_turn()
        events = await await_until(turn, what="steered turn completion")
        assert len(wire.steering_frames()) == 1
        assert sum(event.kind == EVENT_STEER_CONSUMED for event in events) == 1
        assert [item["content"] for item in slot._queue] == ["later"]
        assert held_interrupts(slot) == []
