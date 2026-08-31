"""Between-turn Claude frames render when they arrive, not on the next prompt.

The defect these pin (observed live in chat-1926): a session worked for 25
minutes with nothing consuming its stream, so 51 rows queued in the client's
inbox and were drained by the NEXT prompt — appended below a message that had
not caused them and stamped with the drain time rather than their own.

Every test drives the real ``AcpClient`` routing path with a fake transport,
because the property at stake is which of two destinations a frame takes.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)


def _client(tmp_path: Path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._session_id = "sess-1"
    client._claude_inbox = asyncio.Queue()
    return client


def _text_frame(text: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    )


def _tool_frame(title: str, call_id: str = "t1") -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sess-1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": title,
                "kind": "execute",
                "status": "pending",
            },
        },
    )


def _sink(client: AcpClient) -> list:
    """Register an idle handler and return the list it records into."""
    seen: list = []

    async def handler(event):
        seen.append(event)

    client.set_claude_idle_handler(handler)
    return seen


class TestIdleRouting:
    @pytest.mark.asyncio
    async def test_a_tool_call_renders_instead_of_queueing(self, tmp_path):
        client = _client(tmp_path)
        seen = _sink(client)

        await client._route_claude_frame(_tool_frame("Read Sage result for PR 2138"))

        assert [e.kind for e in seen] == [EVENT_TOOL_CALL]
        assert seen[0].title == "Read Sage result for PR 2138"
        # The whole point: it did NOT also queue, so the next prompt cannot
        # drain and re-render it.
        assert client._claude_inbox.qsize() == 0

    @pytest.mark.asyncio
    async def test_prose_is_cut_at_the_next_tool_call(self, tmp_path):
        """One row per chunk would be unusable, so text accumulates.

        The tool call is the boundary the reader can see without a timer, and
        the prose explains the step that follows it — so it must flush BEFORE
        the tool row, not after.
        """
        client = _client(tmp_path)
        seen = _sink(client)

        await client._route_claude_frame(_text_frame("Now the stale "))
        await client._route_claude_frame(_text_frame("capability records."))
        assert seen == []  # still accumulating

        await client._route_claude_frame(_tool_frame("Inspect inventory edit"))

        assert [e.kind for e in seen] == [EVENT_TEXT_CHUNK, EVENT_TOOL_CALL]
        assert seen[0].text == "Now the stale capability records."

    @pytest.mark.asyncio
    async def test_frames_queue_normally_while_a_dispatch_is_reading(self, tmp_path):
        """A turn's own frames must reach the turn, not the idle sink."""
        client = _client(tmp_path)
        seen = _sink(client)
        client._claude_dispatch_depth = 1

        await client._route_claude_frame(_tool_frame("in-turn tool"))

        assert seen == []
        assert client._claude_inbox.qsize() == 1

    @pytest.mark.asyncio
    async def test_no_handler_leaves_the_old_path_untouched(self, tmp_path):
        """Every non-dashboard caller (app pools, CLI, tests) keeps queueing."""
        client = _client(tmp_path)

        await client._route_claude_frame(_tool_frame("no sink"))

        assert client._claude_inbox.qsize() == 1

    @pytest.mark.asyncio
    async def test_an_unclaimed_frame_still_reaches_the_dispatch(self, tmp_path):
        """Failing toward the inbox is what makes this path additive.

        A permission request has no UI to answer it outside a turn, so the idle
        path must decline it rather than render it and swallow it.
        """
        client = _client(tmp_path)
        seen = _sink(client)
        permission = JsonRpcMessage(
            id=7, method="session/request_permission", params={"sessionId": "sess-1"}
        )

        await client._route_claude_frame(permission)

        assert seen == []
        assert client._claude_inbox.qsize() == 1

    @pytest.mark.asyncio
    async def test_a_render_failure_does_not_kill_the_reader(self, tmp_path):
        """The handler runs ON the reader loop, which owns the session stream."""
        client = _client(tmp_path)

        async def boom(_event):
            raise RuntimeError("sink exploded")

        client.set_claude_idle_handler(boom)

        await client._route_claude_frame(_tool_frame("boom"))

        # Declined rather than raised, so the frame survives for the dispatch.
        assert client._claude_inbox.qsize() == 1


class TestDispatchHandoff:
    @pytest.mark.asyncio
    async def test_thinking_is_not_accumulated(self, tmp_path):
        """Thinking is broadcast-only and never persisted by the turn consumer.

        There is no live socket to broadcast to between turns, so accumulating
        it would build a row nothing else in the product produces.
        """
        client = _client(tmp_path)
        seen = _sink(client)
        thought = JsonRpcMessage(
            method=METHOD_SESSION_UPDATE,
            params={
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "hmm"},
                },
            },
        )

        await client._route_claude_frame(thought)
        await client._route_claude_frame(_tool_frame("after thinking"))

        assert [e.kind for e in seen] == [EVENT_TOOL_CALL]

    @pytest.mark.asyncio
    async def test_pending_prose_is_cut_before_a_turn_opens(self, tmp_path):
        """Otherwise the trailing between-turn text leaks into the turn's own
        first row, which is the attribution bug in miniature."""
        client = _client(tmp_path)
        seen = _sink(client)
        await client._route_claude_frame(_text_frame("half a thought"))
        assert seen == []

        await client._flush_claude_idle_text(client._claude_idle_handler)

        assert [e.kind for e in seen] == [EVENT_TEXT_CHUNK]
        assert seen[0].text == "half a thought"
