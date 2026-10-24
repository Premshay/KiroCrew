"""Dashboard delivery guarantees over Claude's awaited steering lifecycle.

Handshake, wire shape and adapter races are exercised by the upstream request
suite. These checks join that same real lifecycle to the dashboard's pending
ledger and requeue, while retaining the kiro wire-format compatibility checks.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from test_claude_steering_request import _client as _request_client
from test_claude_steering_request import _spawn, _start_turn, _Tasks, _Wire

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import EVENT_STEER_CONSUMED, METHOD_STEER, JsonRpcMessage
from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers, _settle_consumed_steers
from kiro_crew.testing.wait import await_until


def _kiro_client(tmp_path: Path):
    client = AcpClient(work_dir=tmp_path)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._session_id = "sess-1"
    client._steering_advertised = False
    return client


def test_kiro_does_not_need_a_steering_advertisement(tmp_path):
    assert _kiro_client(tmp_path).supports_steer is True


@pytest.mark.asyncio
async def test_kiro_wire_format_is_unchanged(tmp_path):
    client = _kiro_client(tmp_path)
    assert await client.steer("look at the logs") is True
    (written,) = client._process.stdin.write.call_args_list
    frame = json.loads(written.args[0].decode())
    assert frame["method"] == METHOD_STEER
    assert frame["params"] == {
        "sessionId": "sess-1",
        "message": "<user_message>\nlook at the logs\n</user_message>",
    }


def _dashboard(tmp_path, monkeypatch, message):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("test")
    slot._pending_steers = [message]
    return state, slot


@pytest.mark.asyncio
async def test_clean_turn_settles_the_dashboard_ledger_once(tmp_path, monkeypatch):
    message = "actually target the v2 base"
    state, slot = _dashboard(tmp_path, monkeypatch, message)
    async with _Tasks() as spawned:
        client = _request_client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        steer = _spawn(spawned, client.steer(message))
        request_id = await wire.next_steering_id()
        wire.answer(request_id, "injected")
        assert await await_until(steer, what="the accepted steer") is True

        # Acceptance is not consumption. A repeated answer cannot settle it twice.
        assert slot._pending_steers == [message]
        assert (
            client._settle_steering_answer(
                JsonRpcMessage(id=request_id, result={"outcome": "injected"})
            )
            is False
        )
        wire.end_turn()
        events = await await_until(turn, what="the clean turn")
        consumed = [event for event in events if event.kind == EVENT_STEER_CONSUMED]
        assert len(consumed) == 1
        _settle_consumed_steers(slot, consumed[0].text, state)
        _requeue_unconsumed_steers(state, slot)

    assert slot._pending_steers == []
    assert slot._queue == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["promptRequired", "deferredToNextTurn", "startedNewTurn"])
async def test_unconfirmed_outcome_requeues_the_pending_message(tmp_path, monkeypatch, outcome):
    message = "keep this question visible"
    state, slot = _dashboard(tmp_path, monkeypatch, message)
    async with _Tasks() as spawned:
        client = _request_client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        steer = _spawn(spawned, client.steer(message))
        wire.answer(await wire.next_steering_id(), outcome)
        assert await await_until(steer, what="the undelivered steer") is False
        wire.end_turn()
        events = await await_until(turn, what="the completed turn")
        assert not any(event.kind == EVENT_STEER_CONSUMED for event in events)
        assert slot._pending_steers == [message]
        _requeue_unconsumed_steers(state, slot)

    assert slot._pending_steers == []
    assert [item["content"] for item in slot._queue] == [message]


@pytest.mark.asyncio
async def test_cancelled_turn_requeues_an_accepted_steer(tmp_path, monkeypatch):
    message = "do not lose this steer"
    state, slot = _dashboard(tmp_path, monkeypatch, message)
    async with _Tasks() as spawned:
        client = _request_client()
        wire = _Wire(client)
        turn = await _start_turn(client, spawned)
        steer = _spawn(spawned, client.steer(message))
        wire.answer(await wire.next_steering_id(), "injected")
        assert await await_until(steer, what="the accepted steer") is True
        wire.end_turn("cancelled")
        events = await await_until(turn, what="the cancelled turn")
        assert not any(event.kind == EVENT_STEER_CONSUMED for event in events)
        _requeue_unconsumed_steers(state, slot)

    assert slot._pending_steers == []
    assert [item["content"] for item in slot._queue] == [message]
