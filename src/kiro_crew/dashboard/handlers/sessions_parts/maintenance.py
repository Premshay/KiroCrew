"""Session maintenance routes retained alongside upstream lifecycle handlers."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import _sel
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.platform import redact_log_via_context
from kiro_crew.security import redact

logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


_BLOCKER_NOT_A_CHANNEL_WORKER = "not_a_channel_worker"

_BLOCKER_ATTACHED_DASHBOARD_SESSION = "attached_dashboard_session"

_BLOCKER_NOT_BLOCKING = "not_blocking"

_MAX_BLOCKER_CLEAR_KEYS = 64


async def _restart_barrier_status(
    state: DashboardState, *, open_if_busy: bool
) -> dict[str, object]:
    """Refresh the reset barrier against every currently busy ACP session."""
    snapshot = await state.sessions.restart_barrier_snapshot()
    busy: dict[str, float] = {}
    for row in snapshot:
        if row.get("busy") is not True or not isinstance(row.get("session_key"), str):
            continue
        marker = row.get("activity_marker")
        if not isinstance(marker, (int, float)):
            raise ValueError("busy session snapshot lacks a numeric activity marker")
        busy[str(row["session_key"])] = float(marker)
    slot_keys = {effective_session_key(slot) for slot in state._slots.values()}
    managed_busy = {key: marker for key, marker in busy.items() if key in slot_keys}
    unmanaged_busy = set(busy) - slot_keys
    barrier = state.restart_barrier
    if barrier.active:
        barrier.refresh(managed_busy, unmanaged_busy)
    elif open_if_busy and busy:
        barrier.open(managed_busy, unmanaged_busy)
    return barrier.payload()


def _publish_restart_barrier(state: DashboardState) -> None:
    state.broadcast_ws_owners("restart_barrier", state.restart_barrier.payload())


async def api_session_maintenance(request: web.Request) -> web.Response:
    """Read or acknowledge the active coordinated session-reset barrier.

    This endpoint is strict-internal only. The caller key is resolved by the
    MCP process identity; a browser cannot forge an acknowledgement for an
    agent it does not control.
    """
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response(
            {"error": "X-Session-Key required", "code": "missing_session_key"}, status=400
        )
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_request"},
            status=400,
        )
    action = body.get("action")
    if action not in {"status", "acknowledge"}:
        return web.json_response(
            {"error": "action must be status or acknowledge", "code": "invalid_action"},
            status=400,
        )
    status = await _restart_barrier_status(state, open_if_busy=False)
    if action == "acknowledge":
        ok, detail = state.restart_barrier.acknowledge(session_key)
        status = await _restart_barrier_status(state, open_if_busy=False)
        _publish_restart_barrier(state)
        if not ok:
            return web.json_response(
                {"error": detail, "code": "acknowledgement_rejected", "maintenance": status},
                status=409,
            )
    return web.json_response({"ok": True, "maintenance": status})


def _blocker_lock(state: DashboardState) -> LoopBoundLock:
    """Serialise bulk clears so two operators cannot interleave one batch.

    DashboardState can outlive the event loop that first serves this endpoint,
    so a LoopBoundLock prevents a later loop from awaiting a lock bound to a
    closed predecessor.
    """
    lock = getattr(state, "_restart_blocker_lock", None)
    if not isinstance(lock, LoopBoundLock):
        lock = LoopBoundLock()
        state._restart_blocker_lock = lock
    return lock


def _restart_blocker_view(
    state: DashboardState, status: dict[str, object]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Name every slotless session blocking the reset, and who owns it.

    Dashboard-slot sessions are deliberately absent: they acknowledge for
    themselves through ``/api/session-maintenance`` and are reported by the
    barrier's own ``pending`` list.  This split is what keeps a channel-owned
    worker -- which has no operator to acknowledge on its behalf -- from being
    the anonymous "busy slotless worker" that a reset simply refuses on.
    """
    manager = getattr(state, "channel_manager", None)
    finder = getattr(manager, "find_member", None) if manager is not None else None
    channel_blockers: list[dict[str, object]] = []
    other_blockers: list[dict[str, object]] = []
    raw = status.get("unmanaged_busy")
    for key in raw if isinstance(raw, list) else []:
        if not isinstance(key, str):
            continue
        found = finder(key) if callable(finder) else None
        if found is None:
            other_blockers.append({"session_key": key, "reason": _BLOCKER_NOT_A_CHANNEL_WORKER})
            continue
        channel, member = found
        if member.attached_session:
            # An attached member is an operator's own dashboard session that
            # joined a channel. Its reset belongs to the slot that owns it, not
            # to a channel-maintenance control, so no action is offered here.
            other_blockers.append(
                {"session_key": key, "reason": _BLOCKER_ATTACHED_DASHBOARD_SESSION}
            )
            continue
        channel_blockers.append(
            {
                "session_key": key,
                "channel_id": channel.id,
                "channel_topic": channel.topic,
                "agent_id": member.id,
                "role": member.role,
                "agent_name": member.agent_name,
                "state": member.state,
                "is_coordinator": channel.is_active_coordinator(member),
            }
        )
    channel_blockers.sort(key=lambda row: str(row["session_key"]))
    other_blockers.sort(key=lambda row: str(row["session_key"]))
    return channel_blockers, other_blockers


def _blocker_payload(state: DashboardState, status: dict[str, object]) -> dict[str, object]:
    """The wire shape both endpoints answer with."""
    channel_blockers, other_blockers = _restart_blocker_view(state, status)
    return {"channel_blockers": channel_blockers, "other_blockers": other_blockers}


async def api_sessions_restart_blockers(request: web.Request) -> web.Response:
    """GET /api/sessions/restart-blockers — name what is blocking a reset.

    Read-only: it refreshes an ALREADY-OPEN barrier against live work but never
    opens one, so merely viewing the maintenance surface cannot start requiring
    acknowledgements from sessions nobody asked to reset.
    """
    state: DashboardState = request.app["state"]
    status = await _restart_barrier_status(state, open_if_busy=False)
    return web.json_response({"ok": True, "maintenance": status, **_blocker_payload(state, status)})


async def api_sessions_clear_restart_blockers(request: web.Request) -> web.Response:
    """POST /api/sessions/restart-blockers/clear — clear channel workers' context.

    Body: ``{"confirm": true, "session_keys": ["channel:<id>:<agent>", ...]}``.

    Each key runs the channel's own per-worker Clear context lifecycle
    (:func:`kiro_crew.dashboard.handlers_channel.clear_agent_context`), so this
    surface adds reach, not new semantics: channel membership, the shared
    message buffer and exchange counts all survive, and the worker cold-starts
    on its next message.  It is NOT a dismissal and not a cooperative stop.

    The barrier is re-read before every key and once more at the end, so a
    worker that finished on its own between the operator's read and their
    confirmation is skipped rather than reset, and the returned blocker list is
    the state after the batch rather than the state that prompted it.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_request"}, status=400
        )
    keys = body.get("session_keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys):
        return web.json_response(
            {
                "error": "session_keys must be a non-empty list of strings",
                "code": "invalid_request",
            },
            status=400,
        )
    # Deduplicated, but order-preserving: the operator sees results in the order
    # the list was shown to them.
    requested: list[str] = list(dict.fromkeys(keys))
    if len(requested) > _MAX_BLOCKER_CLEAR_KEYS:
        return web.json_response(
            {"error": "too many session keys", "code": "too_many_keys"}, status=400
        )
    if body.get("confirm") is not True:
        return web.json_response(
            {
                "error": "clearing channel worker context requires confirmation",
                "code": "confirmation_required",
            },
            status=409,
        )

    manager = getattr(state, "channel_manager", None)
    if manager is None or not callable(getattr(manager, "find_member", None)):
        return web.json_response(
            {"error": "channels are unavailable", "code": "channels_unavailable"}, status=409
        )

    # Deferred: handlers_channel imports the channel runtime, which this module
    # sits upstream of in the dashboard's import order.
    from kiro_crew.dashboard.handlers_channel import (
        broadcast_context_cleared,
        clear_agent_context,
    )

    results: list[dict[str, object]] = []
    async with _blocker_lock(state):
        status = await _restart_barrier_status(state, open_if_busy=False)
        if status.get("active") is not True:
            blockers = _blocker_payload(state, status)
            return web.json_response(
                {
                    "error": "no coordinated reset is waiting on these sessions",
                    "code": "no_active_barrier",
                    "maintenance": status,
                    "channel_blockers": blockers["channel_blockers"],
                    "other_blockers": blockers["other_blockers"],
                },
                status=409,
            )
        for key in requested:
            # Re-read per key: an earlier clear in this batch, an unrelated
            # reset, or the worker simply finishing all change what is still
            # blocking, and a stale membership row must not authorise a reset.
            status = await _restart_barrier_status(state, open_if_busy=False)
            clearable, unactionable = _restart_blocker_view(state, status)
            row = next(
                (candidate for candidate in clearable if candidate["session_key"] == key), None
            )
            if row is None:
                blocked = next(
                    (candidate for candidate in unactionable if candidate["session_key"] == key),
                    None,
                )
                reason = str(blocked["reason"]) if blocked else _BLOCKER_NOT_BLOCKING
                _sel().log_api_access(
                    caller="dashboard",
                    operation="channel.clear_context",
                    outcome="denied",
                    source="dashboard",
                    resources=f"restart-blocker:{key}:{reason}",
                )
                results.append({"session_key": key, "outcome": "skipped", "reason": reason})
                continue
            found = manager.find_member(key)
            if found is None:
                results.append(
                    {"session_key": key, "outcome": "skipped", "reason": _BLOCKER_NOT_BLOCKING}
                )
                continue
            channel, member = found
            try:
                cleared = await clear_agent_context(state, member)
            except asyncio.CancelledError:
                # The operator's request went away mid-batch. Report nothing as
                # having happened that did not, and let the cancel propagate.
                raise
            except Exception as exc:
                logger.warning(
                    "Clearing channel worker %s during restart maintenance failed",
                    key,
                    exc_info=True,
                )
                _sel().log_api_access(
                    caller="dashboard",
                    operation="channel.clear_context",
                    outcome="denied",
                    source="dashboard",
                    resources=f"restart-blocker:{key}",
                    error=redact_log_via_context(str(exc)),
                )
                results.append(
                    {
                        "session_key": key,
                        "outcome": "failed",
                        "reason": "clear_failed",
                        "detail": redact(str(exc)),
                    }
                )
                continue
            label = member.role or member.id
            _sel().log_api_access(
                caller="dashboard",
                operation="channel.clear_context",
                outcome="allowed",
                source="dashboard",
                resources=f"restart-blocker:{channel.id}:agent:{label}",
            )
            broadcast_context_cleared(channel, "agent", member.id, [label] if cleared else [])
            results.append(
                {
                    "session_key": key,
                    "outcome": "cleared" if cleared else "skipped",
                    "reason": "" if cleared else "no_session",
                    "channel_id": channel.id,
                    "role": member.role,
                }
            )
        status = await _restart_barrier_status(state, open_if_busy=False)
    _publish_restart_barrier(state)
    return web.json_response(
        {"ok": True, "results": results, "maintenance": status, **_blocker_payload(state, status)}
    )


guard_owner_surface_routes(globals(), prefix="api_session", member_scoped=frozenset())
