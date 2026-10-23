"""Bounded session checkpoints, timeline, and restart continuation state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

from kiro_crew.checkpoint_contract import (
    SESSION_CHECKPOINT_ATTENTION_KEY_MAX,
    SESSION_CHECKPOINT_GOAL_MAX,
    SESSION_CHECKPOINT_MAIN_ITEM_MAX,
    SESSION_CHECKPOINT_MAIN_ITEMS_MAX,
    SESSION_CHECKPOINT_MILESTONE_MAX,
    SESSION_CHECKPOINT_NEXT_ACTION_MAX,
    SESSION_CHECKPOINT_PROGRESS_LABEL_MAX,
    SESSION_CHECKPOINT_SUMMARY_MAX,
    SESSION_CHECKPOINT_TRAIL_MAX,
    SESSION_RESTART_CONTINUATION_MAX,
)

_SESSION_TIMELINE_SOURCE_MAX = 32


def init_checkpoint_fields(self: _ChatSlot) -> None:
    self._declared_goal = ""
    self._session_checkpoint = None
    self._session_timeline = []
    self._checkpoint_activity_generation = 0
    self._checkpoint_covered_generation = 0
    self._checkpoint_reminder_generation = 0
    self._checkpoints_this_turn = 0
    self._post_restart_continuation = ""


def _checkpoint_text(value: object, limit: int) -> str:
    from kiro_crew.validation import sanitize_string

    if not isinstance(value, str):
        return ""
    text = sanitize_string(value)
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text[:limit]


def _checkpoint_progress(self: _ChatSlot, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"kind": "none", "completed": 0, "total": 0, "label": ""}
    kind = value.get("kind")
    if kind not in {"plan", "goal"}:
        return {"kind": "none", "completed": 0, "total": 0, "label": ""}
    completed = value.get("completed")
    total = value.get("total")
    if (
        isinstance(completed, bool)
        or not isinstance(completed, int)
        or completed < 0
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 1
        or completed > total
    ):
        return {"kind": "none", "completed": 0, "total": 0, "label": ""}
    return {
        "kind": kind,
        "completed": completed,
        "total": total,
        "label": self._checkpoint_text(
            value.get("label", ""), SESSION_CHECKPOINT_PROGRESS_LABEL_MAX
        ),
    }


def _checkpoint_attention(self: _ChatSlot, value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"status": "none", "kind": "none", "decision_key": ""}
    status = value.get("status")
    kind = value.get("kind")
    decision_key = self._checkpoint_text(
        value.get("decision_key"), SESSION_CHECKPOINT_ATTENTION_KEY_MAX
    )
    if status not in {"none", "unassigned"} or kind not in {"none", "decision"}:
        return {"status": "none", "kind": "none", "decision_key": ""}
    if decision_key and kind != "decision":
        return {"status": "none", "kind": "none", "decision_key": ""}
    return {"status": status, "kind": kind, "decision_key": decision_key}


def _timeline_entry(self: _ChatSlot, value: object) -> dict[str, Any] | None:
    """Normalize one persisted timeline entry without trusting its metadata."""
    if not isinstance(value, dict):
        return None
    text = self._checkpoint_text(value.get("text"), SESSION_CHECKPOINT_MILESTONE_MAX)
    source = self._checkpoint_text(value.get("source"), _SESSION_TIMELINE_SOURCE_MAX)
    timestamp = value.get("timestamp")
    kind = self._checkpoint_text(value.get("kind"), _SESSION_TIMELINE_SOURCE_MAX) or source
    consequence = self._checkpoint_text(
        value.get("consequence"), SESSION_CHECKPOINT_MILESTONE_MAX
    )
    if not text or not source:
        return None
    raw_priority = value.get("priority")
    if isinstance(raw_priority, int) and not isinstance(raw_priority, bool):
        priority = raw_priority
    else:
        priority = self._timeline_default_priority(kind, text)
    return {
        "text": text,
        "source": source,
        "timestamp": timestamp if isinstance(timestamp, str) else "",
        "kind": kind,
        "priority": min(max(priority, 0), 100),
        "consequence": consequence,
    }


def _timeline_default_priority(kind: str, text: str) -> int:
    """Keep legacy lifecycle noise from displacing useful persisted outcomes."""
    if text in {
        "Session started.",
        "Session resumed.",
        "Waiting for tool approval.",
        "Turn cancelled.",
    }:
        return 10
    return {
        "checkpoint": 100,
        "goal": 90,
        "attention": 90,
        "terminal": 90,
        "plan": 80,
        "todo": 80,
        "work": 80,
        "subagents": 20,
        "subagent_activity": 20,
        "session": 10,
        "native_lifecycle": 10,
    }.get(kind, 0)


def _cap_session_timeline(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve important milestones when bounded history is full.

    The oldest least-important entry is removed first, so a burst of routine
    lifecycle messages cannot evict a checkpoint, decision, or actionable
    approval from the operator digest.
    """
    capped = list(entries)
    while len(capped) > SESSION_CHECKPOINT_TRAIL_MAX:
        remove_index = min(
            range(len(capped)), key=lambda index: (capped[index]["priority"], index)
        )
        capped.pop(remove_index)
    return capped


def append_session_timeline(
    self: _ChatSlot,
    text: object,
    source: str,
    *,
    kind: str = "",
    priority: int | None = None,
    consequence: object = "",
) -> bool:
    """Append one redacted structural milestone unless it repeats the current fact."""
    entry = self._timeline_entry(
        {
            "text": text,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "priority": priority,
            "consequence": consequence,
        }
    )
    if entry is None:
        return False
    if self._session_timeline and all(
        self._session_timeline[-1].get(field) == entry[field] for field in ("text", "source")
    ):
        return False
    self._session_timeline = self._cap_session_timeline(self._session_timeline + [entry])
    return True


def restore_session_timeline(self: _ChatSlot, timeline: object) -> None:
    """Restore bounded structural history, keeping only adjacent distinct facts."""
    if not isinstance(timeline, list):
        return
    restored: list[dict[str, Any]] = []
    for value in timeline:
        entry = self._timeline_entry(value)
        if entry is None:
            continue
        if restored and all(
            restored[-1].get(field) == entry[field] for field in ("text", "source")
        ):
            continue
        restored.append(entry)
    self._session_timeline = self._cap_session_timeline(restored)


def session_timeline_payload(self: _ChatSlot) -> list[dict[str, Any]]:
    """Return an isolated bounded timeline for the slots snapshot."""
    return [dict(entry) for entry in self._session_timeline]


def mark_checkpoint_activity(self: _ChatSlot) -> None:
    """Record a meaningful state change without inventing its narrative."""
    self._checkpoint_activity_generation += 1
    self._checkpoints_this_turn = 0


def note_checkpoint_call(self: _ChatSlot) -> int:
    """Count checkpoint calls since the last meaningful activity mark."""
    self._checkpoints_this_turn += 1
    return self._checkpoints_this_turn


def checkpoint_freshness_payload(self: _ChatSlot) -> dict[str, int | bool]:
    """Expose generation state so consumers can flag a stale checkpoint."""
    return {
        "activity_generation": self._checkpoint_activity_generation,
        "covered_generation": self._checkpoint_covered_generation,
        "overdue": self._checkpoint_activity_generation > self._checkpoint_covered_generation,
    }


def restore_checkpoint_freshness(self: _ChatSlot, value: object) -> None:
    """Restore bounded deterministic state, never trusting a stored overdue flag."""
    if not isinstance(value, dict):
        return
    activity = value.get("activity_generation")
    covered = value.get("covered_generation")
    if not isinstance(activity, int) or isinstance(activity, bool) or activity < 0:
        return
    if not isinstance(covered, int) or isinstance(covered, bool) or covered < 0:
        return
    self._checkpoint_activity_generation = min(activity, 1_000_000)
    self._checkpoint_covered_generation = min(covered, self._checkpoint_activity_generation)


def checkpoint_reminder_due(self: _ChatSlot) -> bool:
    # A crewmate's own chat keeps its briefing instead of a Multiplex card.
    if self.mode == "member":
        return False
    return (
        self._checkpoint_activity_generation > self._checkpoint_covered_generation
        and self._checkpoint_reminder_generation < self._checkpoint_activity_generation
    )


def note_checkpoint_reminder(self: _ChatSlot) -> None:
    self._checkpoint_reminder_generation = self._checkpoint_activity_generation


def arm_post_restart_continuation(self: _ChatSlot, checklist: object) -> bool:
    """Arm one explicit post-restart verification turn for this slot."""
    value = self._checkpoint_text(checklist, SESSION_RESTART_CONTINUATION_MAX)
    if not value:
        return False
    self._post_restart_continuation = value
    return True


def restore_post_restart_continuation(self: _ChatSlot, checklist: object) -> None:
    """Restore an explicitly armed verification turn after gateway startup."""
    self._post_restart_continuation = self._checkpoint_text(
        checklist, SESSION_RESTART_CONTINUATION_MAX
    )


def post_restart_continuation(self: _ChatSlot) -> str:
    """Return the pending verification checklist without consuming it."""
    return self._post_restart_continuation


def clear_post_restart_continuation(self: _ChatSlot) -> None:
    """Mark the persisted verification checklist consumed after its turn begins."""
    self._post_restart_continuation = ""


def set_declared_goal(self: _ChatSlot, goal: object) -> bool:
    """Store an owner-declared goal and record its meaningful transition."""
    value = self._checkpoint_text(goal, SESSION_CHECKPOINT_GOAL_MAX)
    if value == self._declared_goal:
        return False
    self._declared_goal = value
    self.mark_checkpoint_activity()
    text = f"Goal declared: {value}" if value else "Goal cleared."
    self.append_session_timeline(text, "goal", kind="goal", priority=90)
    return True


def restore_declared_goal(self: _ChatSlot, goal: object) -> None:
    """Restore a persisted owner-declared goal without inventing an event."""
    self._declared_goal = self._checkpoint_text(goal, SESSION_CHECKPOINT_GOAL_MAX)


def declared_goal_payload(self: _ChatSlot) -> str:
    """Return the bounded goal set by the dashboard owner, if any."""
    return self._declared_goal


def set_session_checkpoint(self: _ChatSlot, checkpoint: dict[str, Any]) -> bool:
    """Replace the concise view and append one bounded milestone."""
    summary = self._checkpoint_text(checkpoint.get("summary"), SESSION_CHECKPOINT_SUMMARY_MAX)
    milestone = self._checkpoint_text(
        checkpoint.get("milestone"), SESSION_CHECKPOINT_MILESTONE_MAX
    )
    if not summary or not milestone:
        raise ValueError("checkpoint summary and milestone are required")
    raw_items = checkpoint.get("main_items")
    main_items = [
        text
        for item in (raw_items if isinstance(raw_items, list) else [])
        if (text := self._checkpoint_text(item, SESSION_CHECKPOINT_MAIN_ITEM_MAX))
    ][:SESSION_CHECKPOINT_MAIN_ITEMS_MAX]
    goal = self._checkpoint_text(checkpoint.get("goal"), SESSION_CHECKPOINT_GOAL_MAX)
    next_action = self._checkpoint_text(
        checkpoint.get("next_action"), SESSION_CHECKPOINT_NEXT_ACTION_MAX
    )
    if "goal" not in checkpoint and self._session_checkpoint is not None:
        goal = self._session_checkpoint["goal"]
    # Attention is part of the current view, like main_items: a checkpoint
    # that omits it has no open request. Carrying it forward left decisions
    # "needing you" for days after they were settled, and no one but the
    # raising agent could clear them.
    attention = self._checkpoint_attention(checkpoint.get("attention"))
    old_trail = self._session_checkpoint.get("trail", []) if self._session_checkpoint else []
    trail = [
        text
        for item in (old_trail if isinstance(old_trail, list) else [])
        if (text := self._checkpoint_text(item, SESSION_CHECKPOINT_MILESTONE_MAX))
    ]
    self._session_checkpoint = {
        "goal": goal,
        "next_action": next_action,
        "summary": summary,
        "main_items": main_items,
        "trail": (trail + [milestone])[-SESSION_CHECKPOINT_TRAIL_MAX:],
        "progress": self._checkpoint_progress(checkpoint.get("progress")),
        "attention": attention,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    self._checkpoint_covered_generation = self._checkpoint_activity_generation
    self._checkpoint_reminder_generation = self._checkpoint_activity_generation
    self.append_session_timeline(milestone, "checkpoint", kind="checkpoint", priority=100)
    return True


def restore_session_checkpoint(self: _ChatSlot, checkpoint: object) -> None:
    """Restore a persisted checkpoint after reapplying display bounds."""
    if not isinstance(checkpoint, dict):
        return
    summary = self._checkpoint_text(checkpoint.get("summary"), SESSION_CHECKPOINT_SUMMARY_MAX)
    if not summary:
        return
    raw_items = checkpoint.get("main_items")
    raw_trail = checkpoint.get("trail")
    updated_at = checkpoint.get("updated_at")
    self._session_checkpoint = {
        "goal": self._checkpoint_text(checkpoint.get("goal"), SESSION_CHECKPOINT_GOAL_MAX),
        "next_action": self._checkpoint_text(
            checkpoint.get("next_action"), SESSION_CHECKPOINT_NEXT_ACTION_MAX
        ),
        "summary": summary,
        "main_items": [
            text
            for item in (raw_items if isinstance(raw_items, list) else [])
            if (text := self._checkpoint_text(item, SESSION_CHECKPOINT_MAIN_ITEM_MAX))
        ][:SESSION_CHECKPOINT_MAIN_ITEMS_MAX],
        "trail": [
            text
            for item in (raw_trail if isinstance(raw_trail, list) else [])
            if (text := self._checkpoint_text(item, SESSION_CHECKPOINT_MILESTONE_MAX))
        ][-SESSION_CHECKPOINT_TRAIL_MAX:],
        "progress": self._checkpoint_progress(checkpoint.get("progress")),
        "attention": self._checkpoint_attention(checkpoint.get("attention")),
        "updated_at": updated_at if isinstance(updated_at, str) else "",
    }
    # Legacy checkpoints predate activity generations. Treat their own
    # restore as current until a new deterministic event occurs.
    self._checkpoint_covered_generation = self._checkpoint_activity_generation
    if not self._session_timeline:
        self.restore_session_timeline(
            [
                {
                    "text": text,
                    "source": "checkpoint",
                    "timestamp": self._session_checkpoint["updated_at"],
                }
                for text in self._session_checkpoint["trail"]
            ]
        )


def session_checkpoint_payload(self: _ChatSlot) -> dict[str, Any] | None:
    """Return an isolated checkpoint copy for the slots snapshot."""
    if self._session_checkpoint is None:
        return None
    return {
        "goal": self._session_checkpoint["goal"],
        "next_action": self._session_checkpoint["next_action"],
        "summary": self._session_checkpoint["summary"],
        "main_items": list(self._session_checkpoint["main_items"]),
        "trail": list(self._session_checkpoint["trail"]),
        "progress": dict(self._session_checkpoint["progress"]),
        "attention": dict(self._session_checkpoint["attention"]),
        "updated_at": self._session_checkpoint["updated_at"],
    }
