"""Focused coverage for the bounded session-checkpoint projection."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.chat_handlers import api_chat_slot_return_handoff
from kiro_crew.dashboard.chat_runner import drain_pending_context
from kiro_crew.dashboard.handlers.sessions import api_session_checkpoint, api_session_maintenance
from kiro_crew.dashboard.restart_barrier import RestartBarrier
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.validation import ValidationError


def _checkpoint(**over: object) -> dict:
    value = {
        "goal": "Make session work visible to operators.",
        "next_action": "Render the next action on the Multiplex card.",
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
    def test_dashboard_router_exports_checkpoint_and_maintenance_handlers(self) -> None:
        assert handlers.api_session_checkpoint is api_session_checkpoint
        assert handlers.api_session_maintenance is api_session_maintenance

    def test_advertises_the_bounded_tool_contract(self) -> None:
        descriptor = next(
            tool for tool in mcp_core._list_tools() if tool["name"] == "session_checkpoint"
        )
        schema = descriptor["inputSchema"]
        assert schema["required"] == ["summary", "milestone"]
        assert schema["properties"]["goal"]["maxLength"] == 240
        assert schema["properties"]["next_action"]["maxLength"] == 160
        assert schema["properties"]["main_items"]["maxItems"] == 4
        assert schema["properties"]["progress"]["additionalProperties"] is False

    def test_emits_a_validated_checkpoint_directive_for_the_calling_session(self, monkeypatch) -> None:
        checkpoint = _checkpoint()
        result = mcp_core._call_tool_inner("session_checkpoint", checkpoint)
        assert result.startswith("Checkpoint requested for this session.")
        assert session_directive.decode(result, "session_checkpoint") == checkpoint

    def test_refuses_an_unverified_session_before_making_a_checkpoint_request(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        result = mcp_core._call_tool_inner("maintenance_acknowledge", {})
        assert result == "Error: maintenance_acknowledge requires a verified session identity."

    def test_rejects_invalid_progress_before_emitting_a_directive(self) -> None:
        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner(
                "session_checkpoint",
                _checkpoint(progress={"kind": "plan", "completed": 3, "total": 2}),
            )

    def test_rejects_an_overlong_goal_before_emitting_a_directive(self) -> None:
        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("session_checkpoint", _checkpoint(goal="x" * 241))

    def test_rejects_an_overlong_next_action_before_emitting_a_directive(self) -> None:
        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("session_checkpoint", _checkpoint(next_action="x" * 161))

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
        path, payload = post.call_args.args
        assert (path, payload) == ("/api/session-maintenance", {"action": "acknowledge"})
        assert post.call_args.kwargs["session_key"].startswith("dashboard:")


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
        assert payload["goal"] == "Make session work visible to operators."
        assert payload["next_action"] == "Render the next action on the Multiplex card."
        assert payload["trail"] == [f"Milestone {number}" for number in range(2, 9)]
        assert payload["progress"] == {
            "kind": "plan",
            "completed": 1,
            "total": 3,
            "label": "Checkpoint slice",
        }
        assert slot.to_dict()["session_checkpoint"] == payload

    def test_checkpoint_goal_persists_until_explicitly_replaced_or_cleared(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot.set_session_checkpoint(_checkpoint(goal="Explain work state."))
        update_without_goal = _checkpoint(summary="Second state.", milestone="Advanced.")
        update_without_goal.pop("goal")
        slot.set_session_checkpoint(update_without_goal)
        assert slot.session_checkpoint_payload()["goal"] == "Explain work state."
        slot.set_session_checkpoint(_checkpoint(goal="", summary="Third state.", milestone="Cleared."))
        assert slot.session_checkpoint_payload()["goal"] == ""

    def test_checkpoint_next_action_clears_when_a_later_checkpoint_omits_it(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot.set_session_checkpoint(_checkpoint(next_action="Run focused tests."))
        update = _checkpoint(summary="Tests are running.", milestone="Verification started.")
        update.pop("next_action")

        slot.set_session_checkpoint(update)

        assert slot.session_checkpoint_payload()["next_action"] == ""

    def test_slot_snapshot_exposes_only_the_structured_plan_goal(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot._plan_goal = "Show session intent without transcript summaries."
        assert slot.to_dict()["plan_goal"] == "Show session intent without transcript summaries."

    def test_declared_goal_is_distinct_from_agent_checkpoint_and_plan_goal(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot._plan_goal = "Parsed plan objective."
        slot.set_session_checkpoint(_checkpoint(goal="Agent checkpoint objective."))

        assert slot.set_declared_goal("Owner-declared objective.") is True
        snapshot = slot.to_dict()
        assert snapshot["declared_goal"] == "Owner-declared objective."
        assert snapshot["plan_goal"] == "Parsed plan objective."
        assert snapshot["session_checkpoint"]["goal"] == "Agent checkpoint objective."
        assert (
            slot.session_timeline_payload()[-1]["text"]
            == "Goal declared: Owner-declared objective."
        )
        assert slot.session_timeline_payload()[-1]["priority"] == 90

    def test_clearing_declared_goal_does_not_clear_agent_checkpoint(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot.set_session_checkpoint(_checkpoint(goal="Agent checkpoint objective."))
        slot.set_declared_goal("Owner-declared objective.")

        assert slot.set_declared_goal("") is True
        assert slot.declared_goal_payload() == ""
        assert slot.session_checkpoint_payload()["goal"] == "Agent checkpoint objective."
        assert slot.session_timeline_payload()[-1]["text"] == "Goal cleared."

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
        assert payload["goal"] == ""
        assert payload["next_action"] == ""
        assert len(payload["summary"]) == 360
        assert payload["main_items"] == ["one", "two", "three", "four"]
        assert payload["trail"] == [f"step {number}" for number in range(3, 10)]
        assert payload["progress"]["kind"] == "none"

    def test_checkpoint_milestone_also_updates_the_redacted_timeline(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot.set_session_checkpoint(_checkpoint(milestone="Recorded AKIAIOSFODNN7EXAMPLE in a draft."))

        timeline = slot.session_timeline_payload()
        assert timeline[0]["source"] == "checkpoint"
        assert timeline[0]["kind"] == "checkpoint"
        assert timeline[0]["priority"] == 100
        assert "AKIAIOSFODNN7EXAMPLE" not in timeline[0]["text"]
        assert "[REDACTED: credential]" in timeline[0]["text"]
        assert timeline[0]["timestamp"]

    def test_timeline_is_bounded_and_suppresses_adjacent_repeated_facts(self) -> None:
        slot = _ChatSlot("checkpoint")
        assert slot.append_session_timeline("Plan updated: 3 steps.", "plan") is True
        assert slot.append_session_timeline("Plan updated: 3 steps.", "plan") is False
        for number in range(9):
            slot.append_session_timeline(f"TODO progress: {number} of 9 complete.", "todo")

        timeline = slot.session_timeline_payload()
        assert len(timeline) == 7
        assert [entry["text"] for entry in timeline] == [
            f"TODO progress: {number} of 9 complete." for number in range(2, 9)
        ]

    def test_timeline_keeps_bounded_operator_digest_metadata(self) -> None:
        slot = _ChatSlot("checkpoint")
        assert slot.append_session_timeline(
            "Approval needed: git.",
            "attention",
            kind="attention",
            priority=95,
            consequence="Awaiting your approval.",
        ) is True
        assert slot.session_timeline_payload()[0]["priority"] == 95
        assert slot.session_timeline_payload()[0]["consequence"] == "Awaiting your approval."
        slot.restore_session_timeline([
            {"text": "Malformed priority.", "source": "terminal", "priority": "high"},
        ])

        timeline = slot.session_timeline_payload()
        assert timeline == [{
            "text": "Malformed priority.",
            "source": "terminal",
            "timestamp": "",
            "kind": "terminal",
            "priority": 90,
            "consequence": "",
        }]

    def test_timeline_retains_high_signal_entries_over_lifecycle_noise(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot.append_session_timeline("Recorded deployment outcome.", "checkpoint")
        for number in range(7):
            slot.append_session_timeline(f"Session resumed: {number}.", "session")

        timeline = slot.session_timeline_payload()
        assert len(timeline) == 7
        assert timeline[0]["text"] == "Recorded deployment outcome."
        assert timeline[0]["priority"] == 100
        assert "Session resumed: 0." not in [entry["text"] for entry in timeline]

    def test_todo_transition_names_the_completed_work_and_next_item(self) -> None:
        from kiro_crew.dashboard.chat_runner import _todo_timeline_entry

        milestone = _todo_timeline_entry(
            {
                "tasks": [
                    {"id": "one", "text": "Install hooks", "completed": False},
                    {"id": "two", "text": "Verify card", "completed": False},
                ],
                "current": "Install hooks",
            },
            {
                "tasks": [
                    {"id": "one", "text": "Install hooks", "completed": True},
                    {"id": "two", "text": "Verify card", "completed": False},
                ],
                "current": "Verify card",
            },
        )

        assert milestone == ("Completed: Install hooks.", "Next: Verify card.")

    def test_restore_migrates_legacy_checkpoint_trail_when_no_timeline_exists(self) -> None:
        slot = _ChatSlot("checkpoint")
        slot.restore_session_checkpoint(_checkpoint(trail=["Planned the slice.", "Ran focused tests."]))

        assert [entry["text"] for entry in slot.session_timeline_payload()] == [
            "Planned the slice.",
            "Ran focused tests.",
        ]


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


class TestReturnHandoff:
    @pytest.mark.asyncio
    async def test_queues_and_records_a_handoff_for_an_active_loop(self, tmp_path, monkeypatch) -> None:
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("loop")
        save = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.save_slot_off_loop", save)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.get_autonudge_instance",
            lambda: SimpleNamespace(get_by_slot=lambda _key: SimpleNamespace(active=True)),
        )
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": "loop"}
        request.get.side_effect = lambda _key, default="": default
        request.json = AsyncMock(return_value={"instruction": "Resume with the reviewed plan."})

        response = await api_chat_slot_return_handoff(request)

        assert response.status == 200
        assert json.loads(response.text) == {"ok": True, "pending": 1}
        save.assert_awaited_once_with(state, slot, force=True)
        assert "[End of background context]\n" in drain_pending_context(slot)
        assert slot.session_timeline_payload()[-1]["text"] == "Human handoff delivered to the next turn."

    def test_expired_handoff_records_non_delivery(self) -> None:
        slot = _ChatSlot("loop")
        slot._pending_context.append(
            {
                "content": "Resume.",
                "source": "multiplex-return-handoff",
                "returnHandoff": True,
                "injectedAt": 0,
                "maxAge": 1,
            }
        )

        assert drain_pending_context(slot) == ""
        assert slot.session_timeline_payload()[-1]["text"] == "Human handoff expired before delivery."

    @pytest.mark.asyncio
    async def test_refuses_a_handoff_when_the_loop_is_not_active(self, tmp_path, monkeypatch) -> None:
        state = _make_state(tmp_path)
        state.get_or_create_slot("loop")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.get_autonudge_instance",
            lambda: SimpleNamespace(get_by_slot=lambda _key: None),
        )
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": "loop"}
        request.get.side_effect = lambda _key, default="": default
        request.json = AsyncMock(return_value={"instruction": "Resume."})

        response = await api_chat_slot_return_handoff(request)

        assert response.status == 409
        assert json.loads(response.text)["code"] == "no_active_loop"


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
        assert restored.session_timeline_payload() == slot.session_timeline_payload()

    def test_declared_goal_survives_save_and_rehydrate(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("declared-goal")
        slot.set_declared_goal("Make the dashboard intent explicit.")
        slot.append("user", "hello")
        slot.drain()
        _save_slot_to_history(state, slot)
        del state._slots["declared-goal"]

        restored = _rehydrate_slot_from_history(state, "declared-goal")
        assert restored is not None
        assert restored.declared_goal_payload() == "Make the dashboard intent explicit."
