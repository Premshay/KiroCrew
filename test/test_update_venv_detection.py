"""Tests for venv detection in dashboard update handler."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions.restart_barrier_snapshot = AsyncMock(return_value=[])
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _track_errors(state):
    errors: list[str] = []
    original = state.push_update_progress

    def track(step, detail=""):
        if step == "error":
            errors.append(detail)
        original(step, detail)

    state.push_update_progress = track
    return errors


class TestRestartGateway:
    """Tests for the _restart_gateway helper."""

    @pytest.mark.asyncio
    async def test_busy_session_blocks_gateway_self_restart(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard.handlers.updates import _restart_gateway
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(monkeypatch, tmp_path)
        state._slots = {"owner": _ChatSlot("owner")}
        state.sessions.restart_barrier_snapshot = AsyncMock(
            return_value=[{"session_key": "dashboard:owner", "busy": True, "activity_marker": 1.0}]
        )
        execv_called: list[bool] = []
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(True))

        result = await _restart_gateway(state)

        assert execv_called == []
        assert result.blocked is True
        assert result.maintenance == state.restart_barrier.payload()
        assert state._update_progress == {
            "step": "blocked",
            "detail": "Gateway restart is waiting for session acknowledgements; "
            "no sessions were stopped.",
            "maintenance": result.maintenance,
        }
        assert state.restart_barrier.payload()["pending"] == ["dashboard:owner"]

    @pytest.mark.asyncio
    async def test_invalid_executable_pushes_error_and_returns(self, monkeypatch, tmp_path) -> None:
        """If sys.executable is missing or not executable, error is reported."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        errors = _track_errors(state)

        execv_called: list[bool] = []

        monkeypatch.setattr("sys.executable", "/nonexistent/python")
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(True))

        await _restart_gateway(state)

        assert execv_called == []
        assert any("Cannot restart" in e for e in errors), errors

    @pytest.mark.asyncio
    async def test_success_path_invokes_execv(self, monkeypatch, tmp_path) -> None:
        """Happy path: history saved, sessions closed, os.execv invoked."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.restart_barrier_snapshot = AsyncMock(return_value=[])
        state.sessions.close_all = AsyncMock(return_value=None)

        save_called: list[bool] = []
        execv_called: list[tuple] = []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.save_all_slots_to_history",
            lambda s: save_called.append(True),
        )
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        # Skip the 0.5s drain delay.
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        await _restart_gateway(state)

        assert save_called == [True]
        assert execv_called, "os.execv should have been called"

    @pytest.mark.asyncio
    async def test_save_history_failure_is_swallowed(self, monkeypatch, tmp_path) -> None:
        """If save_all_slots_to_history raises, restart still proceeds."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.restart_barrier_snapshot = AsyncMock(return_value=[])
        state.sessions.close_all = AsyncMock(return_value=None)

        execv_called: list[tuple] = []

        def raising_save(s):
            raise RuntimeError("boom")

        monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", raising_save)
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        await _restart_gateway(state)
        assert execv_called, "os.execv should have been called even if save raises"

    @pytest.mark.asyncio
    async def test_session_close_failure_is_swallowed(self, monkeypatch, tmp_path) -> None:
        """If sessions.close_all raises, restart still proceeds."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.restart_barrier_snapshot = AsyncMock(return_value=[])
        state.sessions.close_all = AsyncMock(side_effect=RuntimeError("net down"))

        execv_called: list[tuple] = []

        monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", lambda s: None)
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        await _restart_gateway(state)
        assert execv_called, "os.execv should have been called even if close raises"

    @pytest.mark.asyncio
    async def test_concurrent_restart_is_coalesced_before_session_drain(
        self, monkeypatch, tmp_path
    ) -> None:
        """Only one caller may drain and exec a gateway process at a time."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        execv_called: list[tuple] = []

        async def blocking_close() -> None:
            entered.set()
            await release.wait()

        state.sessions = MagicMock()
        state.sessions.restart_barrier_snapshot = AsyncMock(return_value=[])
        state.sessions.close_all = blocking_close
        monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", lambda s: None)
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        first = asyncio.create_task(_restart_gateway(state))
        await entered.wait()
        assert (await _restart_gateway(state)).restarted is False
        release.set()
        assert (await first).restarted is True
        assert len(execv_called) == 1
