"""Operator controls for the channel-owned workers that block a coordinated reset."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.channel import ChannelManager
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers.sessions import (
    _blocker_lock,
    api_sessions_clear_restart_blockers,
    api_sessions_restart_blockers,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.loop_lock import LoopBoundLock


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    sessions.reset = AsyncMock(return_value=True)
    sessions.restart_barrier_snapshot = AsyncMock(return_value=[])
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.broadcast_ws = MagicMock()
    state.channel_manager = ChannelManager(
        broadcast_fn=MagicMock(), channels_dir=str(tmp_path / "channels")
    )
    return state


def _channel_with_workers(state, *roles: str):
    channel = state.channel_manager.create("Restart maintenance")
    return channel, [channel.add_agent(role=role, task=f"work on {role}") for role in roles]


def _snapshot(*keys: str) -> AsyncMock:
    """A live-session snapshot in which every named key currently owns a turn."""
    return AsyncMock(
        return_value=[
            {"session_key": key, "busy": True, "activity_marker": float(index)}
            for index, key in enumerate(keys)
        ]
    )


def _request(state, body=None):
    request = MagicMock()
    request.app = {"state": state}
    request.json = AsyncMock(return_value=body if body is not None else {})
    return request


@pytest.fixture
def audit(monkeypatch):
    recorder = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)
    return recorder


class TestNamingTheBlockers:
    @pytest.mark.asyncio
    async def test_names_a_busy_channel_worker_with_its_channel_and_state(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        channel, (worker,) = _channel_with_workers(state, "Researcher")
        worker.state = "tool_running"
        state.sessions.restart_barrier_snapshot = _snapshot(worker.session_key)
        state.restart_barrier.open({}, {worker.session_key})

        response = await api_sessions_restart_blockers(_request(state))

        assert response.status == 200
        body = json.loads(response.text)
        assert body["channel_blockers"] == [
            {
                "session_key": worker.session_key,
                "channel_id": channel.id,
                "channel_topic": "Restart maintenance",
                "agent_id": worker.id,
                "role": "Researcher",
                "agent_name": "",
                "state": "tool_running",
                "is_coordinator": True,
            }
        ]
        assert body["other_blockers"] == []

    @pytest.mark.asyncio
    async def test_leaves_a_busy_dashboard_slot_to_its_own_acknowledgement(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.get_or_create_slot("operator")
        _channel, (worker,) = _channel_with_workers(state, "Researcher")
        state.sessions.restart_barrier_snapshot = _snapshot(
            "dashboard:operator", worker.session_key
        )
        state.restart_barrier.open({"dashboard:operator": 0.0}, {worker.session_key})

        body = json.loads((await api_sessions_restart_blockers(_request(state))).text)

        # The slot is still the barrier's business, not this control's: it stays
        # in `pending` and gets no clear action offered on its behalf.
        assert body["maintenance"]["pending"] == ["dashboard:operator"]
        assert [row["session_key"] for row in body["channel_blockers"]] == [worker.session_key]

    @pytest.mark.asyncio
    async def test_offers_no_action_for_an_attached_dashboard_session(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        channel, _ = _channel_with_workers(state, "Researcher")
        channel.attach_session(session_key="dashboard:analyst", role="Analyst")
        state.sessions.restart_barrier_snapshot = _snapshot("dashboard:analyst")
        state.restart_barrier.open({}, {"dashboard:analyst"})

        body = json.loads((await api_sessions_restart_blockers(_request(state))).text)

        assert body["channel_blockers"] == []
        assert body["other_blockers"] == [
            {"session_key": "dashboard:analyst", "reason": "attached_dashboard_session"}
        ]

    @pytest.mark.asyncio
    async def test_names_a_slotless_worker_that_belongs_to_no_channel(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.sessions.restart_barrier_snapshot = _snapshot("task:runner-7")
        state.restart_barrier.open({}, {"task:runner-7"})

        body = json.loads((await api_sessions_restart_blockers(_request(state))).text)

        assert body["other_blockers"] == [
            {"session_key": "task:runner-7", "reason": "not_a_channel_worker"}
        ]

    @pytest.mark.asyncio
    async def test_reading_the_surface_never_opens_a_barrier(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        _channel, (worker,) = _channel_with_workers(state, "Researcher")
        state.sessions.restart_barrier_snapshot = _snapshot(worker.session_key)

        body = json.loads((await api_sessions_restart_blockers(_request(state))).text)

        assert state.restart_barrier.active is False
        assert body["maintenance"]["active"] is False
        assert body["channel_blockers"] == []


class TestClearingTheBlockers:
    @pytest.mark.asyncio
    async def test_clears_a_busy_worker_through_the_channel_lifecycle(
        self, tmp_path, audit
    ) -> None:
        state = _make_state(tmp_path)
        channel, (worker,) = _channel_with_workers(state, "Researcher")
        state.sessions.restart_barrier_snapshot = _snapshot(worker.session_key)
        state.restart_barrier.open({}, {worker.session_key})

        async def _drain(key: str) -> bool:
            state.sessions.restart_barrier_snapshot = _snapshot()
            return True

        state.sessions.reset = AsyncMock(side_effect=_drain)
        request = _request(state, {"confirm": True, "session_keys": [worker.session_key]})

        response = await api_sessions_clear_restart_blockers(request)

        assert response.status == 200
        body = json.loads(response.text)
        assert body["results"] == [
            {
                "session_key": worker.session_key,
                "outcome": "cleared",
                "reason": "",
                "channel_id": channel.id,
                "role": "Researcher",
            }
        ]
        state.sessions.reset.assert_awaited_once_with(worker.session_key)
        # The worker keeps its membership: clearing context is not a dismissal.
        assert worker.id in channel.members
        # Refreshed after the clear, so the barrier now reports nothing pending.
        assert body["maintenance"]["ready"] is True
        assert body["channel_blockers"] == []
        assert audit.log_api_access.call_args.kwargs["outcome"] == "allowed"
        assert audit.log_api_access.call_args.kwargs["operation"] == "channel.clear_context"

    @pytest.mark.asyncio
    async def test_refuses_an_unconfirmed_batch(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        _channel, (worker,) = _channel_with_workers(state, "Researcher")
        state.sessions.restart_barrier_snapshot = _snapshot(worker.session_key)
        state.restart_barrier.open({}, {worker.session_key})

        response = await api_sessions_clear_restart_blockers(
            _request(state, {"session_keys": [worker.session_key]})
        )

        assert response.status == 409
        assert json.loads(response.text)["code"] == "confirmation_required"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refuses_when_no_reset_is_waiting(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        _channel, (worker,) = _channel_with_workers(state, "Researcher")
        state.sessions.restart_barrier_snapshot = _snapshot(worker.session_key)

        response = await api_sessions_clear_restart_blockers(
            _request(state, {"confirm": True, "session_keys": [worker.session_key]})
        )

        assert response.status == 409
        assert json.loads(response.text)["code"] == "no_active_barrier"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_a_worker_that_finished_before_the_operator_confirmed(
        self, tmp_path, audit
    ) -> None:
        state = _make_state(tmp_path)
        _channel, (busy, idle) = _channel_with_workers(state, "Researcher", "Reviewer")
        state.sessions.restart_barrier_snapshot = _snapshot(busy.session_key)
        state.restart_barrier.open({}, {busy.session_key, idle.session_key})

        response = await api_sessions_clear_restart_blockers(
            _request(state, {"confirm": True, "session_keys": [idle.session_key]})
        )

        assert json.loads(response.text)["results"] == [
            {"session_key": idle.session_key, "outcome": "skipped", "reason": "not_blocking"}
        ]
        state.sessions.reset.assert_not_awaited()
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_refuses_to_reset_an_attached_dashboard_session(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        channel, _ = _channel_with_workers(state, "Researcher")
        channel.attach_session(session_key="dashboard:analyst", role="Analyst")
        state.sessions.restart_barrier_snapshot = _snapshot("dashboard:analyst")
        state.restart_barrier.open({}, {"dashboard:analyst"})

        response = await api_sessions_clear_restart_blockers(
            _request(state, {"confirm": True, "session_keys": ["dashboard:analyst"]})
        )

        assert json.loads(response.text)["results"] == [
            {
                "session_key": "dashboard:analyst",
                "outcome": "skipped",
                "reason": "attached_dashboard_session",
            }
        ]
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reports_a_failed_clear_and_keeps_going(self, tmp_path, audit) -> None:
        state = _make_state(tmp_path)
        _channel, (first, second) = _channel_with_workers(state, "Researcher", "Reviewer")
        state.sessions.restart_barrier_snapshot = _snapshot(first.session_key, second.session_key)
        state.restart_barrier.open({}, {first.session_key, second.session_key})

        async def _reset(key: str) -> bool:
            if key == first.session_key:
                raise RuntimeError("provider shutdown timed out")
            return True

        state.sessions.reset = AsyncMock(side_effect=_reset)

        response = await api_sessions_clear_restart_blockers(
            _request(
                state,
                {"confirm": True, "session_keys": [first.session_key, second.session_key]},
            )
        )

        results = json.loads(response.text)["results"]
        assert results[0]["outcome"] == "failed"
        assert results[0]["reason"] == "clear_failed"
        assert "provider shutdown timed out" in results[0]["detail"]
        assert results[1]["outcome"] == "cleared"
        assert state.sessions.reset.await_count == 2

    @pytest.mark.asyncio
    async def test_a_cancelled_batch_reports_nothing_it_did_not_do(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        _channel, (first, second) = _channel_with_workers(state, "Researcher", "Reviewer")
        state.sessions.restart_barrier_snapshot = _snapshot(first.session_key, second.session_key)
        state.restart_barrier.open({}, {first.session_key, second.session_key})
        state.sessions.reset = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await api_sessions_clear_restart_blockers(
                _request(
                    state,
                    {"confirm": True, "session_keys": [first.session_key, second.session_key]},
                )
            )

        # The cancel stops the batch at the first key rather than being swallowed
        # and reported as a partial success.
        assert state.sessions.reset.await_count == 1
        assert state.restart_barrier.active is True

    @pytest.mark.asyncio
    async def test_two_concurrent_batches_clear_a_shared_worker_once(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        _channel, (worker,) = _channel_with_workers(state, "Researcher")
        state.sessions.restart_barrier_snapshot = _snapshot(worker.session_key)
        state.restart_barrier.open({}, {worker.session_key})

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_reset(key: str) -> bool:
            started.set()
            await release.wait()
            state.sessions.restart_barrier_snapshot = _snapshot()
            return True

        state.sessions.reset = AsyncMock(side_effect=_slow_reset)
        body = {"confirm": True, "session_keys": [worker.session_key]}
        first = asyncio.create_task(api_sessions_clear_restart_blockers(_request(state, body)))
        await started.wait()
        second = asyncio.create_task(api_sessions_clear_restart_blockers(_request(state, body)))
        await asyncio.sleep(0)
        release.set()
        outcomes = [
            json.loads(response.text)["results"][0]["outcome"]
            for response in await asyncio.gather(first, second)
        ]

        assert outcomes == ["cleared", "skipped"]
        assert state.sessions.reset.await_count == 1

    @pytest.mark.asyncio
    async def test_rejects_a_malformed_batch(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        request = _request(state)
        request.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
        assert (await api_sessions_clear_restart_blockers(request)).status == 400

        for body in ([], {"confirm": True, "session_keys": []}, {"confirm": True}):
            response = await api_sessions_clear_restart_blockers(_request(state, body))
            assert response.status == 400, body

        too_many = {"confirm": True, "session_keys": [f"channel:c:{n}" for n in range(65)]}
        response = await api_sessions_clear_restart_blockers(_request(state, too_many))
        assert json.loads(response.text)["code"] == "too_many_keys"


class TestRouting:
    def test_the_dashboard_router_exports_both_handlers(self) -> None:
        assert handlers.api_sessions_restart_blockers is api_sessions_restart_blockers
        assert handlers.api_sessions_clear_restart_blockers is api_sessions_clear_restart_blockers


class TestBlockerLock:
    def test_long_lived_state_uses_a_loop_bound_lock(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        lock = _blocker_lock(state)

        assert isinstance(lock, LoopBoundLock)

        async def _one_request_loop() -> None:
            async with lock:
                assert lock.locked() is True

        # DashboardState can survive a test-server teardown and be re-used by a
        # new request loop; the lock must bind safely on each loop.
        asyncio.run(_one_request_loop())
        asyncio.run(_one_request_loop())
