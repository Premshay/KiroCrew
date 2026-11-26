"""An approval card names the call its ``tool_call`` stated when the request names none.

dsh asks with ``session/request_permission`` whose ``toolCall`` carries only
``toolCallId``, so the parser gives the permission event the title ``unknown``
(``acp_frames/deepseek/permission-request-live.expected.json``). The chat's own
tool row for the same id already says ``bash``; the approval card and the rows and
audit lines it writes now say the same. The event itself is left as it arrived, so
every decision still reads ``unknown`` exactly as before.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from dashboard_approval_helpers import (
    _answer_approval,
    _complete_event,
    _context_builder,
    _drain,
    _make_slot,
    _make_state,
    _patch_stats,
    _set_stream,
)

from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_turn.permission_prompt import _permission_display_title
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, EVENT_TOOL_CALL, LLMEvent


def _tool_call() -> LLMEvent:
    return LLMEvent(
        kind=EVENT_TOOL_CALL,
        title="bash",
        tool_kind="other",
        tool_call_id="call_0",
        tool_input=json.dumps({"command": "echo probe"}),
    )


def _bare_permission() -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="unknown",
        tool_kind="other",
        tool_call_id="call_0",
        request_id="req-1",
    )


class _Turn:
    def __init__(self, pending: dict[str, str]) -> None:
        self._pending_tools = pending


@pytest.mark.parametrize(
    ("title", "pending", "shown"),
    [
        ("unknown", {"call_0": "bash"}, "bash"),
        ("", {"call_0": "read"}, "read"),
        # A request that names its tool keeps its own title.
        ("Running: ls", {"call_0": "bash"}, "Running: ls"),
        # No row for the id: nothing better to say.
        ("unknown", {}, "unknown"),
        ("unknown", {"other": "bash"}, "unknown"),
    ],
)
def test_the_display_title(title, pending, shown):
    event = LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=title, tool_call_id="call_0")
    assert _permission_display_title(_Turn(pending), event) == shown


@pytest.mark.asyncio
async def test_the_card_and_its_rows_name_the_call_and_the_event_is_untouched(tmp_path):
    state, client = _make_state(tmp_path, context_builder=_context_builder())
    slot = _make_slot("chat-1-deepseek")
    permission = _bare_permission()
    _set_stream(client, [_tool_call(), permission, _complete_event()])
    answer = asyncio.create_task(_answer_approval(slot, "req-1", "approved"))
    try:
        with _patch_stats():
            await _run_chat(state, slot, "hello")
    finally:
        await _drain(answer)

    rows = [m for m in slot.messages if m.get("role") == "permission"]
    assert rows, slot.messages
    assert rows[-1]["content"] == "bash", rows[-1]
    assert any(m.get("content") == "Approved: bash" for m in slot.messages), slot.messages
    assert not any("unknown" in str(m.get("content", "")) for m in slot.messages)
    # Presentation only: the event every decision read is as it arrived.
    assert permission.title == "unknown"
