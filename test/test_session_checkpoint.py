"""Focused coverage for the bounded session-checkpoint projection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import mcp_core, session_directive
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

    def test_returns_a_session_bound_directive(self) -> None:
        checkpoint = _checkpoint()
        result = mcp_core._call_tool_inner("session_checkpoint", checkpoint)
        assert session_directive.decode(result, "session_checkpoint") == checkpoint

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
