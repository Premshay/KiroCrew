"""Provider-native child identity, activity and tool attribution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.types import (
    EVENT_SUBAGENT_ACTIVITY,
    AcpEvent,
    JsonRpcMessage,
    ProviderChildActivity,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger("kiro_crew.acp.client")


def _provider_child_label(update: dict[str, Any], fallback: str) -> str:
    """Return bounded, redacted task context for a provider child card."""
    raw_input = update.get("rawInput")
    label = ""
    if isinstance(raw_input, dict):
        for key in ("description", "prompt", "task"):
            value = raw_input.get(key)
            if isinstance(value, str) and value.strip():
                label = value
                break
    if not label:
        for key in ("description", "name", "workflowName", "taskType", "title"):
            value = update.get(key)
            if isinstance(value, str) and value.strip():
                label = value
                break
    if not label:
        label = fallback
    label = label[:2000]
    label, _ = redact_exfiltration_urls(label)
    label, _ = redact_credentials(label)
    return label


def _provider_child_id(value: object) -> str:
    """Accept only a bounded opaque provider identity for in-process joins."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if 0 < len(value) <= 512 else ""


def _extract_provider_child_activity(
    self: AcpClient, msg: JsonRpcMessage
) -> ProviderChildActivity | None:
    """Recognize documented Claude/Codex child metadata without tool-prose parsing."""
    params = msg.params or {}
    update = params.get("update", {})
    if not isinstance(update, dict):
        return None
    kind = update.get("sessionUpdate")
    if self._is_claude and kind in {
        "async_task_spawned",
        "async_task_progress",
        "async_task_state_update",
    }:
        task_id = self._provider_child_id(update.get("asyncTaskId"))
        if task_id:
            if kind == "async_task_spawned":
                phase = "started"
            elif kind == "async_task_progress":
                phase = "activity"
            else:
                state = str(update.get("state") or "").lower()
                if state in {"completed", "complete", "succeeded", "success", "done"}:
                    phase = "completed"
                elif state in {"failed", "error"}:
                    phase = "failed"
                elif state in {"stopped", "cancelled", "canceled", "interrupted"}:
                    phase = "stopped"
                else:
                    phase = "activity"
            return ProviderChildActivity(
                provider="claude",
                child_id=f"async:{task_id}",
                phase=phase,
                label=self._provider_child_label(update, "Claude workflow"),
            )
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return None

    claude = meta.get("claudeCode")
    if isinstance(claude, dict):
        tool_call_id = self._provider_child_id(update.get("toolCallId"))
        if claude.get("subagent") is True and tool_call_id:
            status = str(update.get("status") or "").lower()
            if status == "completed":
                phase = "completed"
            elif status in {"failed", "error"}:
                phase = "failed"
            elif status in {"cancelled", "canceled", "interrupted"}:
                phase = "stopped"
            else:
                phase = "started"
            return ProviderChildActivity(
                provider="claude",
                child_id=tool_call_id,
                phase=phase,
                label=self._provider_child_label(update, "claude"),
            )
        parent_tool_use_id = self._provider_child_id(claude.get("parentToolUseId"))
        if parent_tool_use_id:
            return ProviderChildActivity(
                provider="claude",
                child_id=parent_tool_use_id,
                phase="activity",
            )

    codex = meta.get("codex")
    subagent = codex.get("subagent") if isinstance(codex, dict) else None
    if isinstance(subagent, dict):
        thread_id = self._provider_child_id(subagent.get("threadId"))
        activity = subagent.get("activity")
        if thread_id and activity in {"started", "interacted", "interrupted"}:
            return ProviderChildActivity(
                provider="codex",
                child_id=thread_id,
                phase="started" if activity == "started" else "activity",
            )
    return None


def _scrub_provider_child_tool_event(
    event: AcpEvent | None, child: ProviderChildActivity | None
) -> AcpEvent | None:
    """Keep adapter-only Codex thread metadata out of ordinary tool surfaces."""
    if event is None or child is None or child.provider != "codex":
        return event
    event.title = "Subagent activity"
    event.tool_input = ""
    event.tool_input_redacted = False
    event.raw_tool_params = None
    event.tool_purpose = ""
    return event


def _provider_child_event(
    msg: JsonRpcMessage,
    child: ProviderChildActivity,
    *,
    text: str = "",
) -> AcpEvent:
    params = msg.params or {}
    update = params.get("update", {})
    tool_call_id = update.get("toolCallId") if isinstance(update, dict) else ""
    title = ""
    if isinstance(update, dict) and update.get("sessionUpdate") == "async_task_progress":
        raw_title = update.get("lastToolName") or update.get("summary")
        title = raw_title if isinstance(raw_title, str) else ""
    return AcpEvent(
        kind=EVENT_SUBAGENT_ACTIVITY,
        text=text,
        title=title,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else "",
        sub_session_id=f"provider:{child.provider}:{child.child_id}",
        provider_child=child,
    )
