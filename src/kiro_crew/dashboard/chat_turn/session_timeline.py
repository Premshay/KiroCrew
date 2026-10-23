"""Session progress and exceptional lifecycle milestones."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        STOP_REASON_CANCELLED,
        STOP_REASON_END_TURN,
        _ChatSlot,
    )


def _record_terminal_timeline(slot: "_ChatSlot", stop_reason: str | None) -> None:
    """Advance checkpoint freshness and record exceptional terminal outcomes."""
    reason = stop_reason or ""
    if reason == STOP_REASON_CANCELLED:
        return
    slot.mark_checkpoint_activity()
    if reason in ("", STOP_REASON_END_TURN, "stop", "completed"):
        return
    if "timeout" in reason:
        text = "Turn timed out."
        consequence = "Verify partial work before resuming."
    else:
        text = "Turn ended with an error."
        consequence = "Inspect the conversation before retrying."
    slot.append_session_timeline(
        text,
        "terminal",
        kind="terminal",
        priority=90,
        consequence=consequence,
    )


def _approval_timeline_entry(operation: str) -> tuple[str, str]:
    """Describe a human blocker even when the provider omits its operation label."""
    if operation.strip().lower() in {"", "unknown", "tool", "command"}:
        return (
            "Approval required; operation not supplied by provider.",
            "Open the conversation to inspect and approve or reject it.",
        )
    return f"Approval needed: {operation}.", "Awaiting your approval."


def _todo_timeline_entry(previous: object, current: object) -> tuple[str, str] | None:
    """Return one operator-facing TODO outcome from two structured snapshots."""
    if not isinstance(current, dict):
        return None
    current_tasks = current.get("tasks")
    if not isinstance(current_tasks, list):
        return None
    previous_tasks = previous.get("tasks") if isinstance(previous, dict) else []
    if not isinstance(previous_tasks, list):
        previous_tasks = []
    completed_before = {
        str(task.get("id") or task.get("text") or "")
        for task in previous_tasks
        if isinstance(task, dict) and task.get("completed")
    }
    newly_completed = [
        str(task.get("text") or "").strip()
        for task in current_tasks
        if isinstance(task, dict)
        and task.get("completed")
        and str(task.get("id") or task.get("text") or "") not in completed_before
        and str(task.get("text") or "").strip()
    ]
    current_item = str(current.get("current") or "").strip()
    previous_item = str(previous.get("current") or "").strip() if isinstance(previous, dict) else ""
    if newly_completed:
        consequence = (
            f"Next: {current_item}." if current_item else "All current TODO items are complete."
        )
        return f"Completed: {newly_completed[-1]}.", consequence
    if current_item and current_item != previous_item:
        return f"Current item: {current_item}.", "Structured TODO state changed."
    return None


def _subagent_roster_snapshot(subagents: object) -> tuple[tuple[str, str], ...]:
    """Return identity and status only; task text is not a lifecycle fact."""
    if not isinstance(subagents, list):
        return ()
    roster: list[tuple[str, str]] = []
    for subagent in subagents:
        if not isinstance(subagent, dict):
            continue
        session_id = str(subagent.get("sessionId") or "")
        status = subagent.get("status")
        kind = str(status.get("type") or "") if isinstance(status, dict) else ""
        if session_id:
            roster.append((session_id, kind.lower()))
    return tuple(sorted(roster))


def _record_subagent_roster_timeline(
    slot: "_ChatSlot", roster: tuple[tuple[str, str], ...]
) -> None:
    """Record a bounded count after a meaningful native-crew roster transition."""
    slot.mark_checkpoint_activity()
    active = sum(
        status in ("", "working", "running", "pending", "queued", "in_progress")
        for _, status in roster
    )
    slot.append_session_timeline(
        f"Subagents: {active} active of {len(roster)}.",
        "subagents",
        kind="subagent_activity",
        priority=20,
    )
