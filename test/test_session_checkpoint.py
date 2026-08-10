"""Focused coverage for the bounded session-checkpoint projection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import json

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.dashboard.handlers.sessions import api_session_checkpoint, api_session_maintenance
from kiro_crew.dashboard.restart_barrier import RestartBarrier
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.validation import ValidationError


def _checkpoint(**over: object) -> dict:
    value = {
        "summary": "Implementing the bounded checkpoint writer.",
        "main_items": ["Add the session directive", "Persist the projection"],
        "milestone": "Mapped the existing Multiplex projection seam.",
        "progress": {"kind": "plan", "completed": 1, "total": 3, "label": "Checkpoint slice"},
    }
    value.update(over)
    return value


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    sessions.restart_barrier_snapshot = AsyncMock(return_value=[])
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


class TestCheckpointDirectiveDispatch:
    def test_advertises_the_bounded_tool_contract(self) -> None:
        descriptor = next(
            tool for tool in mcp_core._list_tools() if tool["name"] == "session_checkpoint"
        )
        schema = descriptor["inputSchema"]
        assert schema["required"] == ["summary", "milestone"]
        assert schema["properties"]["main_items"]["maxItems"] == 4
        assert schema["properties"]["progress"]["additionalProperties"] is False

    def test_persists_through_the_strict_internal_checkpoint_route(self, monkeypatch) -> None:
        checkpoint = _checkpoint()
        post = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(mcp_core, "_post", post)
        result = mcp_core._call_tool_inner("session_checkpoint", checkpoint)
        assert result == "Checkpoint recorded for this session's Multiplex view."
        post.assert_called_once_with(
            "/api/session-checkpoint", checkpoint, require_strict_session=True
        )

    def test_refuses_an_unverified_session_before_making_a_checkpoint_request(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        result = mcp_core._post("/api/session-checkpoint", {}, require_strict_session=True)
        assert result["code"] == "unverified_session"

    def test_rejects_invalid_progress_before_emitting_a_directive(self) -> None:
        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner(
                "session_checkpoint",
                _checkpoint(progress={"kind": "plan", "completed": 3, "total": 2}),
            )

    def test_outer_mcp_boundary_rejects_unknown_progress_fields(self) -> None:
        result = mcp_core._call_tool(
            "session_checkpoint",
            _checkpoint(
                progress={
                    "kind": "plan",
                    "completed": 1,
                    "total": 3,
                    "unexpected": "field",
                }
            ),
        )
        assert result.startswith("Error:")
        assert session_directive.decode(result, "session_checkpoint") is None

    def test_advertises_maintenance_tools(self) -> None:
        names = {tool["name"] for tool in mcp_core._list_tools()}
        assert {"maintenance_status", "maintenance_acknowledge"} <= names

    def test_acknowledges_maintenance_through_the_strict_internal_route(self, monkeypatch) -> None:
        post = MagicMock(return_value={"ok": True, "maintenance": {"ready": True}})
        monkeypatch.setattr(mcp_core, "_post", post)

        result = mcp_core._call_tool_inner("maintenance_acknowledge", {})

        assert result.startswith("Reset acknowledgement recorded")
        post.assert_called_once_with(
            "/api/session-maintenance",
            {"action": "acknowledge"},
            require_strict_session=True,
        )


class TestCheckpointSlotProjection:
    def test_replaces_current_view_and_caps_the_milestone_trail(self) -> None:
        slot = _ChatSlot("checkpoint")
        for number in range(9):
            slot.set_session_checkpoint(
                _checkpoint(summary=f"Current step {number}", milestone=f"Milestone {number}")
            )

        payload = slot.session_checkpoint_payload()
        assert payload is not None
        assert payload["summary"] == "Current step 8"
        assert payload["trail"] == [f"Milestone {number}" for number in range(2, 9)]
        assert payload["progress"] == {
            "kind": "plan",
            "completed": 1,
            "total": 3,
            "label": "Checkpoint slice",
        }
        assert slot.to_dict()["session_checkpoint"] == payload

    def test_restore_drops_malformed_progress_and_keeps_bounded_text(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot.restore_session_checkpoint(
            {
                "summary": "x" * 500,
                "main_items": ["one", "two", "three", "four", "five"],
                "trail": [f"step {number}" for number in range(10)],
                "progress": {"kind": "goal", "completed": 4, "total": 3},
                "updated_at": "2026-08-09T10:00:00+00:00",
            }
        )

        payload = slot.session_checkpoint_payload()
        assert payload is not None
        assert len(payload["summary"]) == 360
        assert payload["main_items"] == ["one", "two", "three", "four"]
        assert payload["trail"] == [f"step {number}" for number in range(3, 10)]
        assert payload["progress"]["kind"] == "none"


class TestCheckpointApplier:
    @pytest.mark.asyncio
    async def test_updates_only_the_consumer_slot_and_pushes_a_snapshot(self) -> None:
        pushes: list[bool] = []
        state = SimpleNamespace(
            conversation_log=None,
            push_slots_update=lambda: pushes.append(True),
        )
        slot = _ChatSlot("consumer")

        result = await apply_session_directive(
            state, slot, "dashboard:consumer", "session_checkpoint", _checkpoint()
        )

        assert "recorded" in result.lower()
        assert slot.session_checkpoint_payload() is not None
        assert pushes == [True]


class TestCheckpointInternalEndpoint:
    def test_route_is_strict_internal_only(self) -> None:
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

        assert "/api/session-checkpoint" in _STRICT_INTERNAL_API_PATHS
        assert "/api/session-maintenance" in _STRICT_INTERNAL_API_PATHS

    @pytest.mark.asyncio
    async def test_persists_only_the_verified_callers_slot(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("consumer")
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:consumer"}
        request.json = AsyncMock(return_value=_checkpoint())

        response = await api_session_checkpoint(request)

        assert response.status == 200
        assert json.loads(response.text)["ok"] is True
        assert slot.session_checkpoint_payload() is not None

    @pytest.mark.asyncio
    async def test_rejects_a_session_without_its_own_live_slot(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        state.get_or_create_slot("consumer")
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:other"}
        request.json = AsyncMock(return_value=_checkpoint())

        response = await api_session_checkpoint(request)

        assert response.status == 404
        assert json.loads(response.text)["code"] == "checkpoint_slot_not_found"

    @pytest.mark.asyncio
    async def test_requires_a_post_barrier_checkpoint_before_acknowledgement(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("consumer")
        state.sessions.restart_barrier_snapshot = AsyncMock(
            return_value=[{"session_key": "dashboard:consumer", "busy": True}]
        )
        state.restart_barrier.open({"dashboard:consumer": 0.0}, set())
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:consumer"}
        request.json = AsyncMock(return_value={"action": "acknowledge"})

        refused = await api_session_maintenance(request)

        assert refused.status == 409
        assert json.loads(refused.text)["code"] == "acknowledgement_rejected"
        checkpoint = MagicMock()
        checkpoint.app = {"state": state}
        checkpoint.headers = {"X-Session-Key": "dashboard:consumer"}
        checkpoint.json = AsyncMock(return_value=_checkpoint())
        assert (await api_session_checkpoint(checkpoint)).status == 200

        accepted = await api_session_maintenance(request)

        assert accepted.status == 200
        assert json.loads(accepted.text)["maintenance"]["ready"] is True
        assert slot.session_checkpoint_payload() is not None


class TestRestartBarrier:
    def test_newly_busy_session_needs_a_checkpoint_after_the_barrier_opens(self) -> None:
        barrier = RestartBarrier()
        barrier.open({"dashboard:one": 0.0}, set())
        barrier.note_checkpoint("dashboard:one")
        assert barrier.acknowledge("dashboard:one") == (True, "acknowledgement recorded")
        barrier.refresh({"dashboard:one": 0.0, "dashboard:two": 0.0}, set())

        assert barrier.pending() == ["dashboard:two"]
        assert barrier.ready() is False

    def test_slotless_busy_worker_is_never_counted_as_acknowledged(self) -> None:
        barrier = RestartBarrier()
        barrier.open({}, {"subagent:worker"})

        assert barrier.ready() is False
        assert barrier.payload()["unmanaged_busy"] == ["subagent:worker"]

    def test_new_turn_after_acknowledgement_requires_another_checkpoint(self) -> None:
        barrier = RestartBarrier()
        barrier.open({"dashboard:one": 1.0}, set())
        barrier.note_checkpoint("dashboard:one")
        assert barrier.acknowledge("dashboard:one") == (True, "acknowledgement recorded")

        barrier.refresh({"dashboard:one": 2.0}, set())

        assert barrier.pending() == ["dashboard:one"]


class TestCheckpointPersistence:
    def test_checkpoint_survives_save_and_rehydrate(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("checkpoint")
        slot.set_session_checkpoint(_checkpoint())
        slot.append("user", "hello")
        slot.drain()
        _save_slot_to_history(state, slot)
        expected = slot.session_checkpoint_payload()
        del state._slots["checkpoint"]

        restored = _rehydrate_slot_from_history(state, "checkpoint")
        assert restored is not None
        assert restored.session_checkpoint_payload() == expected
