"""Claude's between-turn work keeps its session off the idle sweep.

The defect these pin (observed live in chat-1918): a session worked through
Claude background-task continuations until 09:31 and launched another
background waiter, yet the sweep expired it at 09:43 as idle. ``last_used``
moves only when KiroCrew dispatches a turn, and none of that work was one.

The fix reuses the two mechanisms the sweep already honours: the
``sessions.touch`` that ``/api/session-keepalive`` gives a long ``wait``, and
the dashboard's idle-expiry guard. The client half drives the real
``AcpClient`` routing path; the sweep half drives the real ``SessionManager``.
"""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient, ClaudeAutonomousTurn
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _touch_for_background_work
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager


def _client(tmp_path: Path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._session_id = "sess-1"
    client._claude_inbox = asyncio.Queue()
    return client


def _sdk(message: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="_claude/sdkMessage", params={"sessionId": "sess-1", "message": message}
    )


def _task(subtype: str, task_id: str = "bg-1", **extra) -> JsonRpcMessage:
    return _sdk({"type": "system", "subtype": subtype, "task_id": task_id, **extra})


def _text_frame(text: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    )


class TestClientSignals:
    def test_the_task_lifecycle_is_requested_from_the_adapter(self, tmp_path):
        """Without the subscription the adapter never forwards these frames."""
        filters = _client(tmp_path)._claude_session_meta()["claudeCode"][
            "emitRawSDKMessages"
        ]
        subtypes = {f.get("subtype") for f in filters if f.get("type") == "system"}
        assert subtypes == {"task_started", "task_notification", "task_updated"}

    @pytest.mark.asyncio
    async def test_a_started_task_is_outstanding_until_notified(self, tmp_path):
        client = _client(tmp_path)

        await client._route_claude_frame(_task("task_started"))
        assert client.has_background_work is True

        await client._route_claude_frame(_task("task_notification", status="completed"))
        assert client.has_background_work is False

    @pytest.mark.asyncio
    async def test_a_terminal_patch_settles_a_task_without_a_notification(
        self, tmp_path
    ):
        """The adapter guarantees only the patch per transition."""
        client = _client(tmp_path)
        await client._route_claude_frame(_task("task_started"))

        await client._route_claude_frame(
            _task("task_updated", patch={"status": "killed"})
        )

        assert client.has_background_work is False

    @pytest.mark.asyncio
    async def test_a_running_patch_revives_a_resumed_task(self, tmp_path):
        client = _client(tmp_path)

        await client._route_claude_frame(
            _task("task_updated", patch={"status": "running"})
        )

        assert client.has_background_work is True

    @pytest.mark.asyncio
    async def test_lifecycle_frames_never_reach_the_inbox(self, tmp_path):
        client = _client(tmp_path)

        await client._route_claude_frame(_task("task_started"))

        assert client._claude_inbox.qsize() == 0


def _provider() -> AsyncMock:
    m = AsyncMock()
    m.context_usage_pct = lambda: 0.0
    m.has_active_turn = lambda: False
    return m


def _claude_provider(*, background_work: bool) -> MagicMock:
    provider = MagicMock(spec=AcpProvider)
    provider.is_claude_backend = True
    provider.client = SimpleNamespace(has_background_work=background_work)
    return provider


@pytest.fixture
def manager():
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2

    def factory(session_key=None, **kwargs):
        return _provider()

    return SessionManager(cfg, provider_factory=factory)


async def _stale_session(mgr: SessionManager) -> None:
    await mgr.get_or_create("dashboard:tab1")
    mgr.release("dashboard:tab1")
    async with mgr._lock:
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic() - 10_000


def _guard_state(mgr, provider, *, subagent_work: bool = False) -> SimpleNamespace:
    """The two attributes ``DashboardState._has_pending_work`` reads."""
    sessions = SimpleNamespace(get_provider=lambda key: provider)
    subagents = SimpleNamespace(has_pending_work_for=lambda key: subagent_work)
    return SimpleNamespace(sessions=sessions, subagents=subagents)


class TestBetweenTurnWorkTouchesTheSession:
    @pytest.mark.asyncio
    async def test_a_between_turn_row_keeps_a_stale_session(self, manager):
        """The chat-1918 regression: working at 09:31, expired at 09:43."""
        await _stale_session(manager)
        state = SimpleNamespace(sessions=manager)

        _touch_for_background_work(state, _ChatSlot("tab1"))
        await manager._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" in manager._sessions
        await manager.close_all()

    @pytest.mark.asyncio
    async def test_both_between_turn_sinks_touch(self, monkeypatch):
        touched: list[str] = []
        state = SimpleNamespace(sessions=SimpleNamespace(touch=touched.append))
        slot = _ChatSlot("tab1")
        monkeypatch.setattr(chat_runner, "save_slot_off_loop", AsyncMock())

        await chat_runner._render_claude_idle_event(
            state, slot, SimpleNamespace(kind="unrendered")
        )
        await chat_runner._persist_claude_autonomous_turn(
            state,
            slot,
            ClaudeAutonomousTurn(
                text="", origin="task-notification", timestamp="", message_id=""
            ),
        )

        assert touched == ["dashboard:tab1", "dashboard:tab1"]

    def test_a_missing_session_is_not_an_error(self):
        state = SimpleNamespace(
            sessions=SimpleNamespace(touch=MagicMock(side_effect=KeyError))
        )

        _touch_for_background_work(state, _ChatSlot("tab1"))


class TestPendingWorkGuard:
    def test_an_unsettled_native_task_is_pending_work(self, manager):
        state = _guard_state(manager, _claude_provider(background_work=True))
        assert DashboardState._has_pending_work(state, "dashboard:tab1") is True

    def test_a_settled_claude_session_is_not(self, manager):
        state = _guard_state(manager, _claude_provider(background_work=False))
        assert DashboardState._has_pending_work(state, "dashboard:tab1") is False

    def test_other_backends_are_not_asked(self, manager):
        """A double answering truthily must not pin a non-Claude session."""
        state = _guard_state(manager, _provider())
        assert DashboardState._has_pending_work(state, "dashboard:tab1") is False

    def test_subagent_work_still_counts(self, manager):
        state = _guard_state(manager, None, subagent_work=True)
        assert DashboardState._has_pending_work(state, "dashboard:tab1") is True

    @pytest.mark.asyncio
    async def test_an_outstanding_background_task_keeps_a_stale_session(self, manager):
        """The waiter launched at 09:31 would have woken the model again."""
        await _stale_session(manager)
        state = _guard_state(manager, _claude_provider(background_work=True))
        manager.set_idle_expiry_guard(
            lambda key: DashboardState._has_pending_work(state, key)
        )

        await manager._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in manager._sessions
        await manager.close_all()

    @pytest.mark.asyncio
    async def test_a_closed_tab_reaps_once_the_task_settles(self, manager):
        """Same contract as KiroCrew sub-agents: pending work holds a closed
        tab's session only until that work is done."""
        await _stale_session(manager)
        client = SimpleNamespace(has_background_work=True)
        provider = _claude_provider(background_work=True)
        provider.client = client
        state = _guard_state(manager, provider)
        manager.set_idle_expiry_guard(
            lambda key: DashboardState._has_pending_work(state, key)
        )
        manager.set_active_dashboard_slots(set())

        await manager._expire_idle(timeout_secs=1)
        assert "dashboard:tab1" in manager._sessions

        client.has_background_work = False
        await manager._expire_idle(timeout_secs=1)
        assert "dashboard:tab1" not in manager._sessions
        await manager.close_all()
