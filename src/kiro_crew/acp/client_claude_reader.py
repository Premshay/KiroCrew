"""Claude background frames, idle activity and autonomous turn delivery.
Instance fields written: ``_claude_autonomous_handler``, ``_claude_autonomous_handler_owner``,
``_claude_autonomous_message_id``, ``_claude_autonomous_origin``,
``_claude_autonomous_session_key``, ``_claude_autonomous_text``, ``_claude_autonomous_timestamp``,
``_claude_folded_message_break``, ``_claude_idle_handler``, ``_claude_idle_text``,
``_claude_inbox``, ``_claude_live_background_tasks``, ``_claude_reader_task``,
``_pending_claude_autonomous_turns``, ``_session_activity_at``.


Instance fields read: ``_claude_dispatch_depth``, ``_session_id``, ``_session_key``.

"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from kiro_crew.acp.types import (
    EVENT_TEXT_CHUNK,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_UPDATE,
    AcpEvent,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger("kiro_crew.acp.client")


@dataclass(frozen=True, slots=True)
class ClaudeAutonomousTurn:
    """A completed non-human Claude SDK cycle awaiting dashboard delivery."""

    __module__ = "kiro_crew.acp.client"

    text: str
    origin: str
    timestamp: str
    message_id: str
    session_key: str = ""


async def set_claude_autonomous_turn_handler(
    self: AcpClient, handler: Callable[[ClaudeAutonomousTurn], Awaitable[None]] | None
) -> None:
    """Set the dashboard sink and replay completed undelivered turns."""
    self._claude_autonomous_handler = handler
    self._claude_autonomous_handler_owner = self._claude_autonomous_owner()
    if handler is not None:
        await self._flush_pending_claude_autonomous_turns()


def set_claude_idle_handler(
    self: AcpClient, handler: Callable[[AcpEvent], Awaitable[None]] | None
) -> None:
    """Set the sink for work arriving between foreground turns."""
    self._claude_idle_handler = handler


def _claude_autonomous_owner(self: AcpClient) -> str:
    return str(self._session_key or "")


def _discard_claude_autonomous_delivery(self: AcpClient, reason: str) -> None:
    pending = len(self._pending_claude_autonomous_turns)
    partial = bool(self._claude_autonomous_origin or self._claude_autonomous_text)
    if pending or partial:
        logger.warning(
            "Discarding Claude autonomous delivery at %s boundary (pending=%d, partial=%s)",
            reason,
            pending,
            partial,
        )
    self._claude_autonomous_origin = None
    self._claude_autonomous_text.clear()
    self._claude_autonomous_timestamp = ""
    self._claude_autonomous_message_id = ""
    self._claude_autonomous_session_key = ""
    self._pending_claude_autonomous_turns.clear()
    self._claude_autonomous_handler = None
    self._claude_autonomous_handler_owner = ""


async def _start_claude_reader(self: AcpClient) -> None:
    """Give a direct Claude session one permanent stdout owner."""
    if self._is_claude:
        task = self._claude_reader_task
        if task is not None and not task.done():
            return
        self._claude_inbox = asyncio.Queue()
        self._claude_reader_task = asyncio.create_task(
            self._claude_reader_loop(),
            name=f"claude-acp-reader:{self._session_id or 'unknown'}",
        )


async def _claude_reader_loop(self: AcpClient) -> None:
    try:
        while True:
            msg = await self._read_message_from_stdout()
            if msg is None:
                continue
            await self._route_claude_frame(msg)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Claude ACP reader stopped for session %s", self._session_id)
        inbox = self._claude_inbox
        if inbox is not None:
            await inbox.put(exc)


async def _route_claude_frame(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Keep autonomous Claude cycles out of foreground dispatches."""
    self._session_activity_at = time.monotonic()
    if msg.is_method(_CLAUDE_SDK_MESSAGE_METHOD):
        await self._handle_claude_sdk_message(msg)
        return
    # A response to one of our own requests always belongs to the dispatch
    # awaiting it. A prompt sent while an autonomous cycle runs is absorbed
    # into that cycle, and its session/prompt response used to be collected
    # here as cycle output and dropped, leaving the turn waiting until the
    # turn ceiling.
    is_response = msg.id is not None and msg.method is None
    if self._claude_autonomous_origin is not None and not is_response:
        await self._collect_claude_autonomous_frame(msg)
        return
    if self._claude_dispatch_depth == 0 and self._claude_idle_handler is not None:
        if await self._emit_claude_idle_frame(msg):
            return
    inbox = self._claude_inbox
    if inbox is not None:
        await inbox.put(msg)


async def _emit_claude_idle_frame(self: AcpClient, msg: JsonRpcMessage) -> bool:
    handler = self._claude_idle_handler
    if handler is None or not msg.is_method(METHOD_SESSION_UPDATE):
        return False
    try:
        tool_event = self._extract_tool_event(msg) or self._extract_tool_call_update(msg)
        if tool_event is not None:
            await self._flush_claude_idle_text(handler)
            await handler(tool_event)
            return True
        chunk, is_thinking = self._extract_text_chunk(msg)
        if chunk and not is_thinking:
            self._claude_idle_text.append(chunk)
            return True
    except Exception:
        logger.warning("idle frame render failed; leaving it for dispatch", exc_info=True)
    return False


async def _flush_claude_idle_text(
    self: AcpClient, handler: Callable[[AcpEvent], Awaitable[None]]
) -> None:
    if not self._claude_idle_text:
        return
    text = "".join(self._claude_idle_text)
    self._claude_idle_text.clear()
    if text.strip():
        await handler(AcpEvent(kind=EVENT_TEXT_CHUNK, text=text))


async def _handle_claude_sdk_message(self: AcpClient, msg: JsonRpcMessage) -> None:
    params = msg.params if isinstance(msg.params, dict) else {}
    sdk_message = params.get("message")
    if not isinstance(sdk_message, dict):
        logger.warning("Ignoring malformed Claude SDK extension frame")
        return
    kind = sdk_message.get("type")
    if kind == "system":
        self._track_claude_background_task(sdk_message)
        return
    origin_data = sdk_message.get("origin")
    origin = origin_data.get("kind") if isinstance(origin_data, dict) else None
    if kind == "user" and origin in _CLAUDE_AUTONOMOUS_ORIGINS:
        if self._claude_dispatch_depth > 0:
            self._claude_folded_message_break = True
            return
        if self._claude_autonomous_origin is not None:
            logger.error(
                "Claude emitted autonomous %s before terminal %s; discarding partial output",
                origin,
                self._claude_autonomous_origin,
            )
        self._claude_autonomous_origin = origin
        self._claude_autonomous_text.clear()
        self._claude_autonomous_timestamp = str(sdk_message.get("timestamp") or "")
        self._claude_autonomous_message_id = ""
        self._claude_autonomous_session_key = self._claude_autonomous_owner()
        return
    if kind == "assistant" and self._claude_autonomous_origin is not None:
        timestamp = sdk_message.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            self._claude_autonomous_timestamp = timestamp
        assistant_message = sdk_message.get("message")
        message_id = assistant_message.get("id") if isinstance(assistant_message, dict) else None
        if isinstance(message_id, str) and message_id:
            self._claude_autonomous_message_id = message_id
        return
    if kind == "result" and self._claude_autonomous_origin is None:
        if self._claude_dispatch_depth == 0 and self._claude_idle_handler is not None:
            await self._flush_claude_idle_text(self._claude_idle_handler)
        return
    active_origin = self._claude_autonomous_origin
    if kind == "result" and active_origin is not None and origin == active_origin:
        turn = ClaudeAutonomousTurn(
            text="".join(self._claude_autonomous_text),
            origin=active_origin,
            timestamp=self._claude_autonomous_timestamp,
            message_id=self._claude_autonomous_message_id,
            session_key=self._claude_autonomous_session_key,
        )
        self._claude_autonomous_origin = None
        self._claude_autonomous_text.clear()
        self._claude_autonomous_timestamp = ""
        self._claude_autonomous_message_id = ""
        self._claude_autonomous_session_key = ""
        if turn.text:
            await self._deliver_claude_autonomous_turn(turn)


def _track_claude_background_task(self: AcpClient, sdk_message: dict) -> None:
    task_id = sdk_message.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return
    subtype = sdk_message.get("subtype")
    if subtype == "task_started":
        self._claude_live_background_tasks.add(task_id)
    elif subtype == "task_notification":
        self._claude_live_background_tasks.discard(task_id)
    elif subtype == "task_updated":
        patch = sdk_message.get("patch")
        status = patch.get("status") if isinstance(patch, dict) else None
        if status in _CLAUDE_TASK_TERMINAL_STATUSES:
            self._claude_live_background_tasks.discard(task_id)
        elif status in ("running", "pending"):
            self._claude_live_background_tasks.add(task_id)


async def _collect_claude_autonomous_frame(self: AcpClient, msg: JsonRpcMessage) -> None:
    if msg.is_method(METHOD_REQUEST_PERMISSION):
        permission = self._build_permission_event(msg)
        if permission is None:
            if msg.id is not None:
                await self._send_error(msg.id, -32600, "invalid request id")
            return
        logger.warning(
            "Denying autonomous Claude permission request %s (tool=%s)",
            permission.request_id,
            permission.title,
        )
        await self.reject_tool(permission.request_id)
        return
    if msg.id is not None and msg.method:
        logger.warning("Rejecting unsupported autonomous Claude server request %s", msg.method)
        await self._reject_unknown_server_request(msg)
        return
    if msg.is_method(METHOD_SESSION_UPDATE):
        text, is_thinking = self._extract_text_chunk(msg)
        if text and not is_thinking:
            self._claude_autonomous_text.append(text)


async def _deliver_claude_autonomous_turn(self: AcpClient, turn: ClaudeAutonomousTurn) -> None:
    owner = self._claude_autonomous_owner()
    if turn.session_key != owner:
        logger.error(
            "Dropping Claude autonomous turn for non-owning session "
            "(turn_owner=%s, current_owner=%s, origin=%s)",
            turn.session_key or "<unbound>",
            owner or "<unbound>",
            turn.origin,
        )
        return
    handler = self._claude_autonomous_handler
    if handler is None or self._claude_autonomous_handler_owner != owner:
        self._queue_pending_claude_autonomous_turn(turn)
        return
    try:
        await handler(turn)
    except Exception:
        logger.exception(
            "Could not deliver completed Claude autonomous turn (origin=%s, chars=%d)",
            turn.origin,
            len(turn.text),
        )
        self._queue_pending_claude_autonomous_turn(turn)


async def _flush_pending_claude_autonomous_turns(self: AcpClient) -> None:
    handler = self._claude_autonomous_handler
    if handler is None:
        return
    while self._pending_claude_autonomous_turns:
        turn = self._pending_claude_autonomous_turns.popleft()
        owner = self._claude_autonomous_owner()
        if turn.session_key != owner or self._claude_autonomous_handler_owner != owner:
            logger.error(
                "Dropping queued Claude autonomous turn at ownership boundary "
                "(turn_owner=%s, current_owner=%s, origin=%s)",
                turn.session_key or "<unbound>",
                owner or "<unbound>",
                turn.origin,
            )
            continue
        try:
            await handler(turn)
        except Exception:
            logger.exception(
                "Could not deliver queued Claude autonomous turn (origin=%s, chars=%d)",
                turn.origin,
                len(turn.text),
            )
            self._pending_claude_autonomous_turns.appendleft(turn)
            return


def _queue_pending_claude_autonomous_turn(self: AcpClient, turn: ClaudeAutonomousTurn) -> None:
    if len(self._pending_claude_autonomous_turns) >= _MAX_PENDING_CLAUDE_AUTONOMOUS_TURNS:
        dropped = self._pending_claude_autonomous_turns.popleft()
        logger.error(
            "Dropping undelivered Claude autonomous turn after queue overflow "
            "(origin=%s, chars=%d)",
            dropped.origin,
            len(dropped.text),
        )
    self._pending_claude_autonomous_turns.append(turn)


_CLAUDE_AUTONOMOUS_ORIGINS = frozenset(
    {"task-notification", "peer", "coordinator", "observer", "observer-activity"}
)


_CLAUDE_TASK_LIFECYCLE_SUBTYPES = frozenset({"task_started", "task_notification", "task_updated"})


_CLAUDE_SDK_MESSAGE_METHOD = "_claude/sdkMessage"


_CLAUDE_TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})


_MAX_PENDING_CLAUDE_AUTONOMOUS_TURNS = 16
