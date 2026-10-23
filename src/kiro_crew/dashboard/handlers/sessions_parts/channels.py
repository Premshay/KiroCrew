"""Session channels routes retained alongside upstream lifecycle handlers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import (
    guard_owner_surface_routes,
    internal_memory_scope,
    member_scope_denied_refusal,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import sanitize_string

_CHAT_PAGE_SLOT_MODES = frozenset({"", "orchestrator"})

_ATTACH_REQUEST_REFUSALS = {
    "already_attached": ("that session is already a channel member", 409),
    "already_requested": ("an attach request for that session is already pending", 409),
    "channel_full": ("channel member limit reached, counting pending requests", 409),
    "invalid_listen_mode": ("listen must be all, mention, or silent", 400),
}

_MEMBER_CHANNEL_ACTIONS = frozenset({"status", "post", "request_attach"})


def _channel_memberships(state: DashboardState, session_key: str) -> list[tuple[Any, Any]]:
    """Return only memberships proven by the gateway-authored caller key."""
    manager = getattr(state, "channel_manager", None)
    channels = getattr(manager, "_channels", None)
    if not isinstance(channels, dict):
        return []
    memberships: list[tuple[Any, Any]] = []
    for channel in channels.values():
        member = next(
            (item for item in channel.members.values() if item.session_key == session_key), None
        )
        if member is not None:
            memberships.append((channel, member))
    return memberships


def _channel_status_payload(state: DashboardState, channel: Any, member: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": channel.id,
        "topic": channel.topic,
        "self": {"id": member.id, "role": member.role},
        "coordinator": channel.is_active_coordinator(member),
        "capacity": {"used": len(channel.members), "max": channel._max_agents},
        "peers": [
            {"id": peer.id, "role": peer.role, "state": peer.state}
            for peer in channel.members.values()
            if peer.id != member.id
        ],
    }
    return payload


async def _spawn_channel_member(state: DashboardState, channel: Any, agent: Any) -> None:
    """Start the bounded channel worker through the same lifecycle as the UI."""
    from kiro_crew.channel import run_channel_agent
    from kiro_crew.dashboard.handlers_channel import _spawn_agent_task

    _spawn_agent_task(
        agent, run_channel_agent(agent, channel, state.sessions, is_yolo=lambda: state._yolo)
    )


def _mark_channel_membership_change(state: DashboardState, session_key: str) -> None:
    """Make a coordinator-authored roster change visible to its next checkpoint."""
    for slot in state._slots.values():
        if effective_session_key(slot) == session_key:
            slot.mark_checkpoint_activity()
            slot._dirty = True
            return


def _attach_target_slot_name(value: object) -> str | None:
    """Accept a live slot name, its ``dashboard:`` key, or its history key."""
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    for prefix in ("dashboard:", "dashboard_"):
        if value.startswith(prefix):
            return value[len(prefix) :] or None
    return value


async def _request_channel_attach(
    state: DashboardState, channel: Any, member: Any, caller_slot: Any, body: dict
) -> web.Response:
    """File a coordinator's attach request; only the dashboard user can grant it.

    ``member`` is the caller's own membership, resolved from its verified session
    key and already checked against ``channel.orchestrator_id``. The requester is
    always that member: nothing in the body can name a different one.
    """
    slot_name = _attach_target_slot_name(body.get("session"))
    if slot_name is None:
        return web.json_response(
            {"error": "session must name a dashboard session", "code": "invalid_attach_target"},
            status=400,
        )
    slot = state._slots.get(slot_name)
    # Same visibility the channel picker applies: an open chat-page session in
    # the caller's own app. A crewmate desk or cron slot is not offered there,
    # so it is not requestable here either.
    if (
        slot is None
        or (caller_slot is not None and slot._app != caller_slot._app)
        or not effective_session_key(slot).startswith("dashboard:")
        or (getattr(slot, "mode", "") or "") not in _CHAT_PAGE_SLOT_MODES
    ):
        return web.json_response(
            {
                "error": "no attachable open dashboard session by that name",
                "code": "slot_not_found",
            },
            status=404,
        )
    if caller_slot is not None and slot is caller_slot:
        return web.json_response(
            {"error": "the caller is already a channel member", "code": "invalid_attach_target"},
            status=400,
        )
    role = body.get("role")
    if role is not None and (not isinstance(role, str) or len(role) > 100):
        return web.json_response(
            {"error": "role must be at most 100 characters", "code": "invalid_worker_role"},
            status=400,
        )
    reason = body.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500:
        return web.json_response(
            {"error": "reason must be at most 500 characters", "code": "invalid_attach_reason"},
            status=400,
        )
    listen_mode = body.get("listen", "all")
    title = sanitize_string(slot.title or slot.key).strip()
    request, code = channel.request_attach(
        requested_by=member.id,
        slot=slot_name,
        title=title,
        role=sanitize_string(role or "").strip() or title,
        listen_mode=listen_mode if isinstance(listen_mode, str) else "",
        reason=sanitize_string(reason).strip(),
    )
    if request is None:
        error, status = _ATTACH_REQUEST_REFUSALS.get(code, ("attach request refused", 409))
        return web.json_response({"error": error, "code": code}, status=status)
    state.push_slots_update()
    return web.json_response({"ok": True, "request": request.to_dict()})


async def api_session_channel(request: web.Request) -> web.Response:
    """Read, report, or coordinate through the caller's own persistent channel."""
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response(
            {"error": "X-Session-Key required", "code": "missing_session_key"}, status=400
        )
    slots = [slot for slot in state._slots.values() if effective_session_key(slot) == session_key]
    if session_key.startswith("dashboard:") and len(slots) != 1:
        return web.json_response(
            {
                "error": "the calling session has no unique live dashboard slot",
                "code": "channel_slot_not_found",
            },
            status=404,
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_channel_request"},
            status=400,
        )
    if getattr(state, "channel_manager", None) is None:
        return web.json_response(
            {"error": "persistent channels are unavailable", "code": "channels_unavailable"},
            status=503,
        )
    attached = _channel_memberships(state, session_key)
    if not attached and not session_key.startswith("dashboard:"):
        return web.json_response(
            {
                "error": "caller is not a persistent channel member",
                "code": "channel_membership_required",
            },
            status=403,
        )
    action = body.get("action")
    if action == "status":
        return web.json_response(
            {
                "ok": True,
                "channels": [
                    _channel_status_payload(state, channel, member) for channel, member in attached
                ],
            }
        )
    if action not in {"post", "add_agent", "remove_member", "request_attach"}:
        return web.json_response(
            {
                "error": "action must be status, post, add_agent, remove_member, or request_attach",
                "code": "invalid_channel_action",
            },
            status=400,
        )
    channel_id = body.get("channel_id")
    if not isinstance(channel_id, str) or len(channel_id) > 32:
        return web.json_response(
            {"error": "channel_id required", "code": "invalid_channel_id"}, status=400
        )
    selected = next(
        ((channel, member) for channel, member in attached if channel.id == channel_id), None
    )
    if selected is None:
        return web.json_response(
            {
                "error": "caller is not attached to that channel",
                "code": "channel_membership_required",
            },
            status=403,
        )
    channel, member = selected
    if action in {"add_agent", "remove_member", "request_attach"}:
        if not channel.is_active_coordinator(member):
            return web.json_response(
                {
                    "error": "only the active channel coordinator may change membership",
                    "code": "channel_coordinator_required",
                },
                status=403,
            )
        if action == "request_attach":
            caller_slot = slots[0] if slots else None
            return await _request_channel_attach(state, channel, member, caller_slot, body)
        if action == "add_agent":
            role = body.get("role")
            task = body.get("task")
            agent_name = body.get("agent", "")
            if not isinstance(role, str) or not role.strip() or len(role) > 100:
                return web.json_response(
                    {"error": "role must be 1 to 100 characters", "code": "invalid_worker_role"},
                    status=400,
                )
            if not isinstance(task, str) or not task.strip() or len(task) > 2000:
                return web.json_response(
                    {"error": "task must be 1 to 2000 characters", "code": "invalid_worker_task"},
                    status=400,
                )
            if not isinstance(agent_name, str) or len(agent_name) > 100:
                return web.json_response(
                    {
                        "error": "agent must be at most 100 characters",
                        "code": "invalid_worker_agent",
                    },
                    status=400,
                )
            if "approval" in body:
                return web.json_response(
                    {
                        "error": "worker approval policy is fixed by the channel safety policy",
                        "code": "worker_approval_policy_fixed",
                    },
                    status=400,
                )
            agent = channel.add_agent(
                role=sanitize_string(role).strip(),
                agent_name=sanitize_string(agent_name).strip(),
                task=sanitize_string(task).strip(),
            )
            if agent is None:
                return web.json_response(
                    {"error": "channel member limit reached", "code": "channel_capacity_reached"},
                    status=409,
                )
            await _spawn_channel_member(state, channel, agent)
            _mark_channel_membership_change(state, session_key)
            state.push_slots_update()
            return web.json_response({"ok": True, "agent": agent.to_dict()})
        member_id = body.get("member_id")
        force = body.get("force", False)
        target = channel.members.get(member_id) if isinstance(member_id, str) else None
        if target is None or target.id == member.id:
            return web.json_response(
                {
                    "error": "select another current channel member",
                    "code": "invalid_channel_member",
                },
                status=400,
            )
        if not isinstance(force, bool):
            return web.json_response(
                {"error": "force must be boolean", "code": "invalid_force_value"}, status=400
            )
        if target.state not in {"done", "failed"} and not force:
            return web.json_response(
                {
                    "error": "only terminal members can be removed without force",
                    "code": "channel_member_active",
                },
                status=409,
            )
        if force and not target.attached_session and target._task and not target._task.done():
            target._task.cancel()
            try:
                await target._task
            except asyncio.CancelledError:
                pass
        channel.remove_agent(target.id, reason="coordinator_removed")
        _mark_channel_membership_change(state, session_key)
        state.push_slots_update()
        return web.json_response({"ok": True, "removed_member_id": target.id})

    recipients = body.get("recipients")
    if (
        not isinstance(recipients, list)
        or not recipients
        or len(recipients) > 8
        or any(not isinstance(recipient, str) for recipient in recipients)
        or len(set(recipients)) != len(recipients)
    ):
        return web.json_response(
            {
                "error": "recipients must be one to eight distinct agent ids",
                "code": "invalid_recipients",
            },
            status=400,
        )
    if member.id in recipients or any(recipient not in channel.members for recipient in recipients):
        return web.json_response(
            {"error": "recipients must name other channel members", "code": "invalid_recipients"},
            status=400,
        )
    content = body.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 4000:
        return web.json_response(
            {"error": "content must be 1 to 4000 characters", "code": "invalid_channel_content"},
            status=400,
        )
    msg_type = body.get("msg_type", "progress")
    if not isinstance(msg_type, str) or msg_type not in {"progress", "mention", "done"}:
        return web.json_response(
            {"error": "invalid message type", "code": "invalid_channel_message_type"}, status=400
        )
    delivery = body.get("delivery", "next_turn")
    if not isinstance(delivery, str) or delivery not in {"next_turn", "interrupt"}:
        return web.json_response(
            {"error": "invalid delivery mode", "code": "invalid_channel_delivery"}, status=400
        )
    if delivery == "interrupt" and msg_type != "mention":
        return web.json_response(
            {
                "error": "interrupt delivery requires a mention",
                "code": "interrupt_requires_mention",
            },
            status=400,
        )
    content, _ = redact_exfiltration_urls(content.strip())
    content, _ = redact_credentials(content)
    message = await channel.post(
        member.id,
        content,
        from_role=member.role,
        mention=recipients,
        msg_type=msg_type,
        delivery=delivery,
    )
    receipts = [
        {"recipient": recipient, "status": message.delivery_status.get(recipient, "not_delivered")}
        for recipient in recipients
    ]
    return web.json_response(
        {
            "ok": True,
            "message": {
                "id": message.id,
                "recipients": recipients,
                "delivery": delivery,
                "receipts": receipts,
            },
        }
    )


async def _member_channel_report_guard(request: web.Request) -> web.Response | None:
    """Let a member read, post and request attaches in its own channels.

    ``request_attach`` only files a card the dashboard user approves, and the
    handler still requires the caller to be that channel's coordinator, so a
    crewmate coordinator can assemble its channel without changing membership
    itself. ``add_agent`` and ``remove_member`` change membership directly and
    stay owner-only.
    """
    scope, refusal = await internal_memory_scope(request, "api_session_channel")
    if refusal is not None or scope is None:
        return refusal
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None  # the handler reports the malformed body
    if isinstance(body, dict) and body.get("action") in _MEMBER_CHANNEL_ACTIONS:
        return None
    return await member_scope_denied_refusal("api_session_channel")


guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(),
    resource_scoped={"api_session_channel": _member_channel_report_guard},
)
