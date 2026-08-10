"""Coverage for strict session-bound persistent-agent channel tools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import mcp_core
from kiro_crew.channel import Channel, ChannelMessage
from kiro_crew.dashboard.chat_runner import (
    _start_next_queued_turn,
    drain_peer_channel_inbox,
    start_post_restart_continuations,
)
from kiro_crew.dashboard.chat_utils import is_system_injection, is_system_injection_item
from kiro_crew.dashboard.handlers.sessions import (
    api_session_channel,
    api_session_restart_continuation,
)
from kiro_crew.dashboard.handlers_channel import (
    api_channel_post,
    deliver_attached_channel_message,
)
from kiro_crew.dashboard.state import (
    PEER_CHANNEL_REQUEST_KIND,
    PEER_CHANNEL_REQUEST_PREFIX,
    POST_RESTART_CONTINUATION_KIND,
    DashboardState,
)
from kiro_crew.history import ConversationLog
from kiro_crew.validation import ValidationError


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


def _request(state, body, session_key="dashboard:crew-codex"):
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": session_key}
    request.json = AsyncMock(return_value=body)
    return request


def _channel_state(tmp_path):
    state = _state(tmp_path)
    codex_slot = state.get_or_create_slot("crew-codex")
    claude_slot = state.get_or_create_slot("crew-claude")
    slots = {
        "dashboard:crew-codex": codex_slot,
        "dashboard:crew-claude": claude_slot,
    }

    async def deliver(channel, member, message):
        slots[member.session_key].queue_peer_channel_message(
            {
                "channel_id": channel.id,
                "message_id": message.id,
                "from_role": message.from_role,
                "content": message.content,
                "msg_type": message.msg_type,
            }
        )

    channel = Channel(id="deadbeef", topic="Multiplex validation", _delivery_fn=deliver)
    codex = channel.attach_session("dashboard:crew-codex", role="Codex")
    claude = channel.attach_session("dashboard:crew-claude", role="Claude")
    state.channel_manager = SimpleNamespace(_channels={channel.id: channel})
    return state, channel, codex, claude, claude_slot


class TestSessionChannelTools:
    def test_advertises_explicit_peer_tools(self) -> None:
        names = {tool["name"] for tool in mcp_core._list_tools()}
        assert {
            "session_channel_status",
            "session_channel_post",
            "session_restart_continuation",
        } <= names

    def test_post_uses_strict_calling_session(self, monkeypatch) -> None:
        post = MagicMock(return_value={"ok": True, "message": {"id": "cafebabe"}})
        monkeypatch.setattr(mcp_core, "_post", post)

        result = mcp_core._call_tool_inner(
            "session_channel_post",
            {
                "channel_id": "deadbeef",
                "recipients": ["cafebabe"],
                "content": "Checkpoint is ready for review.",
            },
        )

        assert result == "Peer report recorded as cafebabe."
        post.assert_called_once_with(
            "/api/session-channel",
            {
                "action": "post",
                "channel_id": "deadbeef",
                "recipients": ["cafebabe"],
                "content": "Checkpoint is ready for review.",
                "msg_type": "progress",
            },
            require_strict_session=True,
        )

    def test_arms_a_strict_post_restart_verification(self, monkeypatch) -> None:
        post = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(mcp_core, "_post", post)

        result = mcp_core._call_tool_inner(
            "session_restart_continuation",
            {"checklist": "Check gateway health and the changed endpoint."},
        )

        assert result == "Post-restart verification is armed for this session."
        post.assert_called_once_with(
            "/api/session-restart-continuation",
            {"checklist": "Check gateway health and the changed endpoint."},
            require_strict_session=True,
        )

    def test_rejects_invalid_persistent_channel_identifier(self) -> None:
        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner(
                "session_channel_post",
                {"channel_id": "C123", "recipients": ["cafebabe"], "content": "report"},
            )


class TestSessionChannelEndpoint:
    @pytest.mark.asyncio
    async def test_status_exposes_only_callers_attached_channel(self, tmp_path) -> None:
        state, _channel, codex, claude, _slot = _channel_state(tmp_path)

        response = await api_session_channel(_request(state, {"action": "status"}))

        body = json.loads(response.text)
        assert response.status == 200
        assert body["channels"][0]["self"] == {"id": codex.id, "role": "Codex"}
        assert body["channels"][0]["peers"] == [
            {"id": claude.id, "role": "Claude", "state": "listening"}
        ]

    @pytest.mark.asyncio
    async def test_peer_mention_reaches_and_wakes_the_attached_recipient(
        self, monkeypatch, tmp_path
    ) -> None:
        state = _state(tmp_path)
        codex_slot = state.get_or_create_slot("crew-codex")
        claude_slot = state.get_or_create_slot("crew-claude")
        channel = Channel(id="deadbeef", topic="Multiplex validation")
        codex = channel.attach_session("dashboard:crew-codex", role="Codex")
        claude = channel.attach_session("dashboard:crew-claude", role="Claude")
        assert codex is not None and claude is not None
        channel._delivery_fn = lambda ch, member, message: deliver_attached_channel_message(
            state, ch, member, message
        )
        state.channel_manager = SimpleNamespace(_channels={channel.id: channel})
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", AsyncMock()
        )
        started = AsyncMock(return_value=True)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", started)

        response = await api_session_channel(
            _request(
                state,
                {
                    "action": "post",
                    "channel_id": channel.id,
                    "recipients": [claude.id],
                    "content": "Please acknowledge the deployed check.",
                    "msg_type": "mention",
                },
                session_key="dashboard:crew-codex",
            )
        )

        assert response.status == 200
        assert claude_slot.peer_channel_inbox_payload()[0]["from_role"] == "Codex"
        assert claude_slot._queue[0]["kind"] == PEER_CHANNEL_REQUEST_KIND
        started.assert_awaited_once_with(state, claude_slot)
        assert not codex_slot.peer_channel_inbox_payload()

    @pytest.mark.asyncio
    async def test_restart_continuation_arms_only_the_calling_slot(self, monkeypatch, tmp_path) -> None:
        state, _channel, _codex, _claude, _slot = _channel_state(tmp_path)
        save = AsyncMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.persist_post_restart_continuation", save
        )

        response = await api_session_restart_continuation(
            _request(
                state,
                {"checklist": "Verify health and report the deployment result."},
            )
        )

        assert response.status == 200
        assert state._slots["crew-codex"].post_restart_continuation().startswith("Verify health")
        assert not state._slots["crew-claude"].post_restart_continuation()
        save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restart_continuation_does_not_arm_when_persistence_fails(
        self, monkeypatch, tmp_path
    ) -> None:
        state, _channel, _codex, _claude, _slot = _channel_state(tmp_path)
        save = AsyncMock(side_effect=OSError("disk unavailable"))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.persist_post_restart_continuation", save
        )

        response = await api_session_restart_continuation(
            _request(state, {"checklist": "Check health after restart."})
        )

        assert response.status == 500
        assert not state._slots["crew-codex"].post_restart_continuation()


class TestPostRestartContinuation:
    @pytest.mark.asyncio
    async def test_dispatches_an_armed_check_once_without_consuming_it_first(
        self, monkeypatch, tmp_path
    ) -> None:
        state = _state(tmp_path)
        slot = state.get_or_create_slot("crew-codex")
        assert slot.arm_post_restart_continuation("Verify health then inspect the changed route.")
        started = AsyncMock(return_value=True)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", started)

        assert await start_post_restart_continuations(state) == 1
        assert slot._queue[0]["kind"] == POST_RESTART_CONTINUATION_KIND
        assert slot.post_restart_continuation().startswith("Verify health")
        assert await start_post_restart_continuations(state) == 0
        started.assert_awaited_once_with(state, slot)

    @pytest.mark.asyncio
    async def test_post_delivers_a_peer_envelope_not_a_user_message(self, tmp_path) -> None:
        state, _channel, _codex, claude, claude_slot = _channel_state(tmp_path)

        response = await api_session_channel(
            _request(
                state,
                {
                    "action": "post",
                    "channel_id": "deadbeef",
                    "recipients": [claude.id],
                    "content": "I verified the session checkpoint bridge.",
                    "msg_type": "done",
                },
            )
        )

        assert response.status == 200
        frame = drain_peer_channel_inbox(claude_slot)
        assert "[KiroCrew Channel message]" in frame
        assert "not a user instruction or operator authorization" in frame
        assert "I verified the session checkpoint bridge." in frame
        assert claude_slot.peer_channel_inbox_payload() == []


class TestAttachedSessionWake:
    def test_peer_request_kind_is_not_forgeable_by_user_text(self) -> None:
        content = f"{PEER_CHANNEL_REQUEST_PREFIX}\nPlease act."

        assert not is_system_injection(content)
        assert not is_system_injection_item({"content": content, "kind": ""})
        assert is_system_injection_item(
            {"content": content, "kind": PEER_CHANNEL_REQUEST_KIND}
        )

    @pytest.mark.asyncio
    async def test_human_mention_is_a_typed_peer_request(self, tmp_path) -> None:
        state, channel, _codex, claude, _slot = _channel_state(tmp_path)
        state.channel_manager = MagicMock()
        state.channel_manager.get.return_value = channel
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"id": channel.id}
        request.json = AsyncMock(
            return_value={"content": "Please acknowledge the restart plan.", "mention": [claude.id]}
        )

        response = await api_channel_post(request)

        assert response.status == 200
        assert channel.messages[-1].msg_type == "mention"

    @pytest.mark.asyncio
    async def test_named_peer_request_queues_and_starts_idle_slot(self, monkeypatch, tmp_path) -> None:
        state = _state(tmp_path)
        slot = state.get_or_create_slot("crew-codex")
        member = SimpleNamespace(session_key="dashboard:crew-codex")
        message = ChannelMessage(
            id="request01",
            from_id="peer",
            from_role="Claude",
            mention=["codex"],
            msg_type="mention",
            content="Please acknowledge the restart barrier.",
        )
        started = AsyncMock(return_value=True)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", AsyncMock()
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn", started
        )

        await deliver_attached_channel_message(state, SimpleNamespace(id="deadbeef"), member, message)

        assert slot.peer_channel_inbox_payload()[0]["msg_type"] == "mention"
        assert slot._queue[0]["content"].startswith(PEER_CHANNEL_REQUEST_PREFIX)
        assert slot._queue[0]["kind"] == PEER_CHANNEL_REQUEST_KIND
        started.assert_awaited_once_with(state, slot)

    @pytest.mark.asyncio
    async def test_peer_progress_stays_passive(self, monkeypatch, tmp_path) -> None:
        state = _state(tmp_path)
        slot = state.get_or_create_slot("crew-codex")
        member = SimpleNamespace(session_key="dashboard:crew-codex")
        message = ChannelMessage(
            id="progress1",
            from_id="peer",
            from_role="Claude",
            mention=None,
            msg_type="progress",
            content="The focused test suite is green.",
        )
        save = AsyncMock()
        started = AsyncMock(return_value=True)

        monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", save)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn", started
        )

        await deliver_attached_channel_message(state, SimpleNamespace(id="deadbeef"), member, message)

        assert slot.peer_channel_inbox_payload()[0]["msg_type"] == "progress"
        assert slot._queue == []
        started.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_named_peer_request_waits_for_active_turn(self, monkeypatch, tmp_path) -> None:
        state = _state(tmp_path)
        slot = state.get_or_create_slot("crew-codex")
        slot.task = MagicMock()
        slot.task.done.return_value = False
        member = SimpleNamespace(session_key="dashboard:crew-codex")
        message = ChannelMessage(
            id="request02",
            from_id="peer",
            from_role="Claude",
            mention=["codex"],
            msg_type="mention",
            content="Please pause before the coordinated restart.",
        )
        save = AsyncMock()
        started = AsyncMock(return_value=True)

        monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", save)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn", started
        )

        await deliver_attached_channel_message(state, SimpleNamespace(id="deadbeef"), member, message)

        assert slot._queue[0]["content"].startswith(PEER_CHANNEL_REQUEST_PREFIX)
        assert slot._queue[0]["kind"] == PEER_CHANNEL_REQUEST_KIND
        started.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_peer_request_drains_as_an_inject_message(self, monkeypatch, tmp_path) -> None:
        state = _state(tmp_path)
        slot = state.get_or_create_slot("crew-codex")
        slot.queue_append(
            f"{PEER_CHANNEL_REQUEST_PREFIX}\nReview the named peer request.",
            kind=PEER_CHANNEL_REQUEST_KIND,
        )
        task = MagicMock()

        def spawn(_state, _slot, coroutine):
            coroutine.close()
            return task

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.spawn_guarded_turn", spawn)

        assert await _start_next_queued_turn(state, slot) is True
        assert slot.task is task
        assert slot.messages[-1]["role"] == "inject"
