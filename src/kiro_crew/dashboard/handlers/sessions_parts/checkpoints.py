"""Session checkpoints routes retained alongside upstream lifecycle handlers."""

from __future__ import annotations

import json
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes
from kiro_crew.dashboard.handlers.sessions_parts.maintenance import _publish_restart_barrier
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.validation import (
    SESSION_CHECKPOINT_SCHEMA,
    SESSION_RESTART_CONTINUATION_SCHEMA,
    ValidationError,
    validate_tool_args,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


async def api_session_checkpoint(request: web.Request) -> web.Response:
    """Persist a checkpoint on the uniquely bound calling dashboard slot."""
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response(
            {"error": "X-Session-Key required", "code": "missing_session_key"}, status=400
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_checkpoint"},
            status=400,
        )
    try:
        checkpoint = validate_tool_args(body, SESSION_CHECKPOINT_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_checkpoint"}, status=400)
    slots = [slot for slot in state._slots.values() if effective_session_key(slot) == session_key]
    if len(slots) != 1:
        return web.json_response(
            {
                "error": "the calling session has no unique live dashboard slot",
                "code": "checkpoint_slot_not_found",
            },
            status=404,
        )
    slot = slots[0]
    if slot.mode == "member":
        return web.json_response(
            {
                "error": "a crewmate's own chat keeps its briefing instead of a checkpoint; "
                "work sessions it opens publish checkpoints",
                "code": "checkpoint_not_for_member_chat",
            },
            status=409,
        )
    try:
        slot.set_session_checkpoint(checkpoint)
        from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

        await save_slot_off_loop(state, slot, force=True, best_effort=False)
    except Exception:
        slot._dirty = True
        logger.warning("session checkpoint persistence failed for %s", slot.key, exc_info=True)
        return web.json_response(
            {
                "error": "checkpoint persistence failed; the gateway will retry the in-memory update",
                "code": "checkpoint_persistence_failed",
            },
            status=500,
        )
    state.restart_barrier.note_checkpoint(session_key)
    if state.restart_barrier.active:
        _publish_restart_barrier(state)
    state.push_slots_update()
    calls = slot.note_checkpoint_call() if hasattr(slot, "note_checkpoint_call") else 1
    return web.json_response(
        {
            "ok": True,
            "session_checkpoint": slot.session_checkpoint_payload(),
            "calls_this_turn": calls,
        }
    )


async def api_session_restart_continuation(request: web.Request) -> web.Response:
    """Arm one post-restart verification turn on the calling dashboard slot."""
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response(
            {"error": "X-Session-Key required", "code": "missing_session_key"}, status=400
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_restart_continuation"},
            status=400,
        )
    try:
        continuation = validate_tool_args(body, SESSION_RESTART_CONTINUATION_SCHEMA)
    except ValidationError as exc:
        return web.json_response(
            {"error": str(exc), "code": "invalid_restart_continuation"}, status=400
        )
    slots = [slot for slot in state._slots.values() if effective_session_key(slot) == session_key]
    if len(slots) != 1:
        return web.json_response(
            {
                "error": "the calling session has no unique live dashboard slot",
                "code": "restart_continuation_slot_not_found",
            },
            status=404,
        )
    slot = slots[0]
    previous_continuation = slot.post_restart_continuation()
    if not slot.arm_post_restart_continuation(continuation["checklist"]):
        return web.json_response(
            {"error": "checklist required", "code": "invalid_restart_continuation"}, status=400
        )
    try:
        from kiro_crew.dashboard.chat_persistence import persist_post_restart_continuation

        await persist_post_restart_continuation(state, slot)
    except Exception:
        slot.restore_post_restart_continuation(previous_continuation)
        logger.warning("restart continuation persistence failed for %s", slot.key, exc_info=True)
        return web.json_response(
            {
                "error": "restart continuation persistence failed",
                "code": "restart_continuation_persistence_failed",
            },
            status=500,
        )
    state.push_slots_update()
    return web.json_response({"ok": True})


guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset({"api_session_checkpoint", "api_session_restart_continuation"}),
)
