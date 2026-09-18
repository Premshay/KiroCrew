"""The stale-turn clock has to cover the wait AFTER a tool returns.

``_stale_eligible`` arms the stale branch of the dispatch loop's watchdog. It is
armed by a text chunk and cleared by a tool call -- correctly, because the tool
clock covers a call that is still in flight. Nothing re-armed it when the tool
RESULT landed, so the gap between the last result and the model's next frame was
covered by no clock at all. A turn whose model never sent the follow-up then sat
outside every watchdog -- rows complete and no terminal event -- and parked the
slot with no probe and no log line to explain it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage

SESSION = "sA"


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle(SESSION, asyncio.Queue(), rt)


def _update_msg(update: dict) -> JsonRpcMessage:
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE, params={"sessionId": SESSION, "update": update}
    )
    msg.fanout_no_owner = False
    return msg


def _tool_call(tool_call_id: str = "tc1") -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "title": "grep",
        "kind": "read",
        "rawInput": {"pattern": "x"},
    }


def _tool_result(tool_call_id: str = "tc1") -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": tool_call_id,
        "status": "completed",
        "content": [{"content": {"type": "text", "text": "ok"}}],
    }


async def _drive(handle: AcpSessionHandle, *frames: JsonRpcMessage) -> None:
    for frame in frames:
        handle._queue.put_nowait(frame)
    handle._queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
    async for _ in handle._dispatch_events(req_id=1, timeout=5.0):
        pass


@pytest.mark.asyncio
async def test_a_tool_result_re_arms_the_stale_clock() -> None:
    handle = _handle()
    await _drive(handle, _update_msg(_tool_call()), _update_msg(_tool_result()))
    assert handle._tool_dispatched is False
    assert handle._stale_eligible is True, "a returned tool leaves the turn waiting on the model"


@pytest.mark.asyncio
async def test_a_dispatched_tool_still_disarms_the_stale_clock() -> None:
    """The re-arm must not let the stale clock judge a tool that is running."""
    handle = _handle()
    await _drive(handle, _update_msg(_tool_call()))
    assert handle._stale_eligible is False
