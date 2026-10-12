"""Coordinator-requested, user-approved attachment of existing dashboard sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import mcp_core
from kiro_crew.channel import Channel
from kiro_crew.dashboard import server
from kiro_crew.dashboard.handlers.sessions import api_session_channel
from kiro_crew.dashboard.handlers_channel import api_channel_resolve_attach_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

_RESOLVE_PATH = "/api/channels/{id}/attach-requests/{rid}"


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


def _session_request(state, body, session_key="dashboard:lead"):
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": session_key}
    request.json = AsyncMock(return_value=body)
    return request


def _operator_request(state, channel_id, request_id, body):
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"id": channel_id, "rid": request_id}
    request.json = AsyncMock(return_value=body)
    request.get = MagicMock(return_value="")
    return request


def _setup(tmp_path):
    state = _state(tmp_path)
    state.get_or_create_slot("lead").title = "Lead"
    state.get_or_create_slot("helper").title = "Helper"
    state.get_or_create_slot("review-lane").title = "Review lane"
    events: list[tuple[str, dict]] = []
    channel = Channel(
        id="deadbeef",
        topic="Attach requests",
        _broadcast_fn=lambda kind, data: events.append((kind, data)),
        _max_agents=3,
    )
    lead = channel.attach_session("dashboard:lead", role="Lead")
    helper = channel.attach_session("dashboard:helper", role="Helper")
    assert lead is not None and helper is not None
    assert channel.orchestrator_id == lead.id
    channels = {channel.id: channel}
    state.channel_manager = SimpleNamespace(_channels=channels, get=channels.get)
    return state, channel, lead, helper, events


async def _file(state, channel, session="review-lane", session_key="dashboard:lead", **extra):
    return await api_session_channel(
        _session_request(
            state,
            {
                "action": "request_attach",
                "channel_id": channel.id,
                "session": session,
                **extra,
            },
            session_key=session_key,
        )
    )


class TestRequestAttach:
    @pytest.mark.asyncio
    async def test_coordinator_files_a_request_without_changing_membership(self, tmp_path):
        state, channel, _lead, _helper, events = _setup(tmp_path)

        response = await _file(
            state, channel, session="dashboard_review-lane", reason="Needs the review lane."
        )

        body = json.loads(response.text)
        assert response.status == 200
        assert body["request"]["slot"] == "review-lane"
        assert body["request"]["role"] == "Review lane"
        assert body["request"]["listen_mode"] == "mention"
        assert len(channel.members) == 2
        assert channel.to_dict()["attach_requests"][0]["reason"] == "Needs the review lane."
        assert events[-1][0] == "channel_attach_requests"

    @pytest.mark.asyncio
    async def test_non_coordinator_cannot_request(self, tmp_path):
        state, channel, _lead, _helper, _events = _setup(tmp_path)

        response = await _file(state, channel, session_key="dashboard:helper")

        assert response.status == 403
        assert json.loads(response.text)["code"] == "channel_coordinator_required"
        assert channel.attach_requests == {}

    @pytest.mark.asyncio
    async def test_refuses_unknown_desk_and_self_targets(self, tmp_path):
        state, channel, _lead, _helper, _events = _setup(tmp_path)
        state.get_or_create_slot("grain-desk").mode = "member"

        unknown = await _file(state, channel, session="no-such-slot")
        desk = await _file(state, channel, session="grain-desk")
        self_target = await _file(state, channel, session="dashboard:lead")

        assert unknown.status == 404
        assert desk.status == 404
        assert json.loads(self_target.text)["code"] in {
            "invalid_attach_target",
            "already_attached",
        }
        assert channel.attach_requests == {}

    @pytest.mark.asyncio
    async def test_pending_requests_count_against_capacity_and_duplicates(self, tmp_path):
        state, channel, _lead, _helper, _events = _setup(tmp_path)
        state.get_or_create_slot("fourth").title = "Fourth"

        first = await _file(state, channel)
        duplicate = await _file(state, channel)
        overflow = await _file(state, channel, session="fourth")

        assert first.status == 200
        assert json.loads(duplicate.text)["code"] == "already_requested"
        assert json.loads(overflow.text)["code"] == "channel_full"

    def test_tool_advertises_request_attach(self):
        manage = next(t for t in mcp_core._list_tools() if t["name"] == "session_channel_manage")
        properties = manage["inputSchema"]["properties"]
        assert "request_attach" in properties["action"]["enum"]
        assert {"session", "listen", "reason"} <= set(properties)

    def test_tool_reports_that_membership_did_not_change(self, monkeypatch):
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:lead")
        monkeypatch.setattr(
            mcp_core,
            "_post",
            MagicMock(
                return_value={"ok": True, "request": {"id": "cafebabe", "title": "Review lane"}}
            ),
        )

        result = mcp_core._call_tool_inner(
            "session_channel_manage",
            {"action": "request_attach", "channel_id": "deadbeef", "session": "review-lane"},
        )

        assert "cafebabe" in result
        assert "Membership has not changed" in result


class TestResolveAttachRequest:
    @pytest.mark.asyncio
    async def test_approve_attaches_and_notifies_the_coordinator(self, tmp_path):
        state, channel, lead, _helper, _events = _setup(tmp_path)
        filed = json.loads((await _file(state, channel, role="Reviewer")).text)["request"]

        response = await api_channel_resolve_attach_request(
            _operator_request(state, channel.id, filed["id"], {"action": "approve"})
        )

        body = json.loads(response.text)
        assert response.status == 200
        assert body["agent"]["role"] == "Reviewer"
        assert body["agent"]["listen_mode"] == "mention"
        assert body["agent"]["session_key"] == "dashboard:review-lane"
        assert channel.attach_requests == {}
        notice = channel.messages[-1]
        assert notice.from_id == "system"
        assert notice.mention == [lead.id]
        assert "approved" in notice.content

    @pytest.mark.asyncio
    async def test_decline_leaves_membership_and_notifies(self, tmp_path):
        state, channel, lead, _helper, _events = _setup(tmp_path)
        filed = json.loads((await _file(state, channel)).text)["request"]

        response = await api_channel_resolve_attach_request(
            _operator_request(state, channel.id, filed["id"], {"action": "decline"})
        )

        assert response.status == 200
        assert len(channel.members) == 2
        assert channel.attach_requests == {}
        assert "declined" in channel.messages[-1].content
        assert channel.messages[-1].mention == [lead.id]

    @pytest.mark.asyncio
    async def test_approve_of_a_closed_session_keeps_the_request(self, tmp_path):
        state, channel, _lead, _helper, _events = _setup(tmp_path)
        filed = json.loads((await _file(state, channel)).text)["request"]
        del state._slots["review-lane"]

        response = await api_channel_resolve_attach_request(
            _operator_request(state, channel.id, filed["id"], {"action": "approve"})
        )

        assert response.status == 404
        assert filed["id"] in channel.attach_requests
        assert len(channel.members) == 2

    @pytest.mark.asyncio
    async def test_rejects_unknown_action_and_request(self, tmp_path):
        state, channel, _lead, _helper, _events = _setup(tmp_path)
        filed = json.loads((await _file(state, channel)).text)["request"]

        bad_action = await api_channel_resolve_attach_request(
            _operator_request(state, channel.id, filed["id"], {"action": "trust"})
        )
        missing = await api_channel_resolve_attach_request(
            _operator_request(state, channel.id, "00000000", {"action": "approve"})
        )

        assert bad_action.status == 400
        assert missing.status == 404


class TestAttachRequestLifecycle:
    @pytest.mark.asyncio
    async def test_coordinator_transfer_withdraws_old_requests(self, tmp_path):
        state, channel, _lead, helper, _events = _setup(tmp_path)
        await _file(state, channel)

        channel.set_coordinator(helper.id)

        assert channel.attach_requests == {}

    @pytest.mark.asyncio
    async def test_requests_persist_and_stale_ones_are_dropped_on_load(self, tmp_path):
        state, channel, _lead, helper, _events = _setup(tmp_path)
        await _file(state, channel)

        restored = Channel.deserialize(channel.serialize())
        assert list(restored.attach_requests) == list(channel.attach_requests)

        data = channel.serialize()
        data["orchestrator_id"] = helper.id
        assert Channel.deserialize(data).attach_requests == {}

    def test_resolve_route_is_operator_only(self):
        # The click is the grant: an agent's internal-secret credential must
        # never reach the route that approves an attach request.
        for internal in server._STRICT_INTERNAL_API_PATHS | server._MIXED_INTERNAL_API_PATHS:
            assert not _RESOLVE_PATH.startswith(internal + "/")
            assert not internal.startswith("/api/channels")
