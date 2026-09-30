"""Claude's between-turn work keeps its session off the idle sweep.

The defect these pin (observed live in chat-1918): a session worked through
Claude background-task continuations until 09:31 and launched another
background waiter, yet the sweep expired it at 09:43 as idle. ``last_used``
moves only when KiroCrew dispatches a turn, and none of that work was one.

The client half drives the real ``AcpClient`` routing path; the sweep half
drives the real ``SessionManager`` with a provider exposing the two signals.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)
from kiro_crew.config import KiroCrewConfig
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
        filters = _client(tmp_path)._claude_session_meta()["claudeCode"]["emitRawSDKMessages"]
        subtypes = {f.get("subtype") for f in filters if f.get("type") == "system"}
        assert subtypes == {"task_started", "task_notification", "task_updated"}

    @pytest.mark.asyncio
    async def test_between_turn_frames_stamp_activity(self, tmp_path):
        client = _client(tmp_path)
        before = time.monotonic()

        await client._route_claude_frame(_text_frame("still working"))

        assert client.background_activity_at is not None
        assert client.background_activity_at >= before

    @pytest.mark.asyncio
    async def test_a_dispatched_turn_does_not_stamp_background_activity(self, tmp_path):
        """A turn is already on the session's own clock."""
        client = _client(tmp_path)
        client._claude_dispatch_depth = 1

        await client._route_claude_frame(_text_frame("in-turn"))

        assert client.background_activity_at is None

    @pytest.mark.asyncio
    async def test_a_started_task_is_outstanding_until_notified(self, tmp_path):
        client = _client(tmp_path)

        await client._route_claude_frame(_task("task_started"))
        assert client.has_background_work is True

        await client._route_claude_frame(_task("task_notification", status="completed"))
        assert client.has_background_work is False

    @pytest.mark.asyncio
    async def test_a_terminal_patch_settles_a_task_without_a_notification(self, tmp_path):
        """The adapter guarantees only the patch per transition."""
        client = _client(tmp_path)
        await client._route_claude_frame(_task("task_started"))

        await client._route_claude_frame(_task("task_updated", patch={"status": "killed"}))

        assert client.has_background_work is False

    @pytest.mark.asyncio
    async def test_a_running_patch_revives_a_resumed_task(self, tmp_path):
        client = _client(tmp_path)

        await client._route_claude_frame(_task("task_updated", patch={"status": "running"}))

        assert client.has_background_work is True

    @pytest.mark.asyncio
    async def test_lifecycle_frames_never_reach_the_inbox(self, tmp_path):
        client = _client(tmp_path)

        await client._route_claude_frame(_task("task_started"))

        assert client._claude_inbox.qsize() == 0


def _provider() -> AsyncMock:
    """The sweep's usual provider double, with both signals set to real values."""
    m = AsyncMock()
    m.context_usage_pct = lambda: 0.0
    m.has_active_turn = lambda: False
    m.background_activity_at = None
    m.has_background_work = False
    return m


@pytest.fixture
def manager():
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    providers: list[AsyncMock] = []

    def factory(session_key=None, **kwargs):
        providers.append(_provider())
        return providers[-1]

    return SessionManager(cfg, provider_factory=factory), providers


async def _stale_session(mgr: SessionManager) -> None:
    await mgr.get_or_create("dashboard:tab1")
    mgr.release("dashboard:tab1")
    async with mgr._lock:
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic() - 10_000


class TestIdleSweep:
    @pytest.mark.asyncio
    async def test_recent_background_activity_keeps_a_stale_session(self, manager):
        """The chat-1918 regression: working at 09:31, expired at 09:43."""
        mgr, providers = manager
        await _stale_session(mgr)
        providers[0].background_activity_at = time.monotonic()

        await mgr._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_old_background_activity_still_expires(self, manager):
        mgr, providers = manager
        await _stale_session(mgr)
        providers[0].background_activity_at = time.monotonic() - 5_000

        await mgr._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_outstanding_background_task_keeps_a_stale_session(self, manager):
        """The waiter launched at 09:31 would have woken the model again."""
        mgr, providers = manager
        await _stale_session(mgr)
        providers[0].has_background_work = True

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_closed_tab_still_reaps_despite_background_work(self, manager):
        """Native work pins the idle axis only, not the orphan axis."""
        mgr, providers = manager
        await _stale_session(mgr)
        providers[0].has_background_work = True
        mgr.set_active_dashboard_slots(set())

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" not in mgr._sessions
        await mgr.close_all()
