"""Work a backend does between turns keeps its session off the idle sweep.

The defect these pin (observed live in chat-1918): a session worked through
Claude background-task continuations until 09:31 and launched another
background waiter, yet the sweep expired it at 09:43 as idle. ``last_used``
moves only when KiroCrew dispatches a turn, and none of that work was one.

The shape is backend-neutral. Each transport stamps the last frame it routed
for a session -- the Claude reader in ``AcpClient``, and ``AcpRuntime`` for
the shared-runtime backends (Codex, kiro-cli), including a child's frames it
drops between turns -- and the sweep measures idleness from the later of that
and the turn clock. Unsettled native Claude tasks, which can wait silently,
are pending work for the dashboard's idle-expiry guard.
"""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.state import DashboardState
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


class TestClaudeActivityClock:
    @pytest.mark.asyncio
    async def test_a_between_turn_frame_stamps_the_session(self, tmp_path):
        client = _client(tmp_path)
        before = time.monotonic()

        await client._route_claude_frame(_text_frame("still working"))

        assert client.session_activity_at is not None
        assert client.session_activity_at >= before

    def test_a_client_without_the_reader_reports_nothing(self, tmp_path):
        """DeepSeek and friends on this transport are not read between turns."""
        assert (
            AcpClient(work_dir=tmp_path, acp_backend="deepseek").session_activity_at
            is None
        )


def _runtime(tmp_path: Path) -> tuple[AcpRuntime, asyncio.StreamReader]:
    rt = AcpRuntime(work_dir=str(tmp_path))
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    return rt, reader


def _update(session_id: str) -> dict:
    return {
        "method": METHOD_SESSION_UPDATE,
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "x"},
            },
        },
    }


async def _route(rt: AcpRuntime, reader: asyncio.StreamReader, *frames: dict) -> None:
    """Feed *frames*, then a sentinel for a session of its own, and wait for it.

    The sentinel is the barrier: the reader handles frames in order, so once its
    stamp lands every frame fed before it has been handled too. That makes a
    NEGATIVE read (nothing stamped) about the behaviour, not about timing.
    """
    rt._session_queues.setdefault("sentinel", asyncio.Queue())
    for frame in (*frames, _update("sentinel")):
        reader.feed_data((json.dumps(frame) + "\n").encode())
    task = asyncio.ensure_future(rt._reader_loop())
    try:
        async with asyncio.timeout(10):
            while rt.session_activity_at("sentinel") is None:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class TestRuntimeActivityClock:
    @pytest.mark.asyncio
    async def test_a_session_frame_stamps_that_session_only(self, tmp_path):
        rt, reader = _runtime(tmp_path)
        rt._session_queues.update({"sA": asyncio.Queue(), "sB": asyncio.Queue()})

        await _route(rt, reader, _update("sA"))

        assert rt.session_activity_at("sA") is not None
        assert rt.session_activity_at("sB") is None

    @pytest.mark.asyncio
    async def test_a_child_frame_dropped_between_turns_still_stamps_its_owner(
        self, tmp_path
    ):
        """The between-turn child frame is discarded, but the child IS working."""
        rt, reader = _runtime(tmp_path)
        rt._session_queues["owner"] = asyncio.Queue()
        rt._subagent_owner = "owner"
        rt._subagent_sessions = {"child"}

        # Only the owner may be registered for the child route, so the
        # sentinel barrier cannot be used here; wait on the owner's stamp.
        reader.feed_data((json.dumps(_update("child")) + "\n").encode())
        task = asyncio.ensure_future(rt._reader_loop())
        try:
            async with asyncio.timeout(10):
                while rt.session_activity_at("owner") is None:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        assert rt._session_queues["owner"].empty()  # dropped, as before

    @pytest.mark.asyncio
    async def test_an_ownerless_broadcast_stamps_nobody(self, tmp_path):
        """One tenant's traffic must not keep every tenant alive."""
        rt, reader = _runtime(tmp_path)
        rt._session_queues.update({"sA": asyncio.Queue(), "sB": asyncio.Queue()})
        ownerless = {"method": METHOD_SESSION_UPDATE, "params": {"update": {}}}

        await _route(rt, reader, ownerless)

        assert rt.session_activity_at("sA") is None
        assert rt.session_activity_at("sB") is None

    def test_unregistering_forgets_the_stamp(self, tmp_path):
        rt, _ = _runtime(tmp_path)
        rt._session_activity_at["sA"] = time.monotonic()

        rt.unregister_session("sA")

        assert rt.session_activity_at("sA") is None


def _provider(activity_at: object = None) -> AsyncMock:
    m = AsyncMock()
    m.context_usage_pct = lambda: 0.0
    m.has_active_turn = lambda: False
    m.session_activity_at = activity_at
    return m


def _claude_provider(*, background_work: bool) -> MagicMock:
    provider = MagicMock(spec=AcpProvider)
    provider.is_claude_backend = True
    provider.client = SimpleNamespace(has_background_work=background_work)
    return provider


@pytest.fixture
def providers():
    return []


@pytest.fixture
def manager(providers):
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2

    def factory(session_key=None, **kwargs):
        providers.append(_provider())
        return providers[-1]

    return SessionManager(cfg, provider_factory=factory)


async def _stale_session(mgr: SessionManager) -> None:
    await mgr.get_or_create("dashboard:tab1")
    mgr.release("dashboard:tab1")
    async with mgr._lock:
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic() - 10_000


class TestIdleSweep:
    @pytest.mark.asyncio
    async def test_recent_backend_activity_keeps_a_stale_session(
        self, manager, providers, caplog
    ):
        """The chat-1918 regression: working at 09:31, expired at 09:43.

        The scan itself must not elect it: the post-probe re-check reads the
        same clock and would rescue it, but only after logging it as expired.
        """
        await _stale_session(manager)
        providers[0].session_activity_at = time.monotonic()

        with caplog.at_level("INFO"):
            await manager._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" in manager._sessions
        assert "expired" not in caplog.text
        await manager.close_all()

    @pytest.mark.asyncio
    async def test_old_backend_activity_still_expires(self, manager, providers):
        await _stale_session(manager)
        providers[0].session_activity_at = time.monotonic() - 5_000

        await manager._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" not in manager._sessions
        await manager.close_all()

    @pytest.mark.asyncio
    async def test_a_non_number_leaves_the_turn_clock_in_charge(
        self, manager, providers
    ):
        """A provider double's auto-attribute must not pin a session."""
        await _stale_session(manager)
        providers[0].session_activity_at = MagicMock()

        await manager._expire_idle(timeout_secs=3600)

        assert "dashboard:tab1" not in manager._sessions
        await manager.close_all()

    def test_acp_provider_forwards_its_transport_stamp(self):
        provider = AcpProvider.__new__(AcpProvider)
        provider._client = SimpleNamespace(session_activity_at=123.0)

        assert provider.session_activity_at == 123.0


def _guard_state(provider, *, subagent_work: bool = False) -> SimpleNamespace:
    """The two attributes ``DashboardState._has_pending_work`` reads."""
    sessions = SimpleNamespace(get_provider=lambda key: provider)
    subagents = SimpleNamespace(has_pending_work_for=lambda key: subagent_work)
    return SimpleNamespace(sessions=sessions, subagents=subagents)


class TestPendingWorkGuard:
    def test_an_unsettled_native_task_is_pending_work(self):
        state = _guard_state(_claude_provider(background_work=True))
        assert DashboardState._has_pending_work(state, "dashboard:tab1") is True

    def test_a_settled_claude_session_is_not(self):
        state = _guard_state(_claude_provider(background_work=False))
        assert DashboardState._has_pending_work(state, "dashboard:tab1") is False

    def test_other_backends_are_not_asked(self):
        """A double answering truthily must not pin a non-Claude session."""
        assert DashboardState._has_pending_work(_guard_state(_provider()), "k") is False

    def test_subagent_work_still_counts(self):
        state = _guard_state(None, subagent_work=True)
        assert DashboardState._has_pending_work(state, "dashboard:tab1") is True

    @pytest.mark.asyncio
    async def test_a_silent_waiter_keeps_a_stale_session(self, manager):
        """The waiter launched at 09:31 emits nothing until it settles."""
        await _stale_session(manager)
        state = _guard_state(_claude_provider(background_work=True))
        manager.set_idle_expiry_guard(
            lambda key: DashboardState._has_pending_work(state, key)
        )

        await manager._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in manager._sessions
        await manager.close_all()
