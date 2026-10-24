"""Caller-bound checkpoints, coordination and maintenance tools.

Resolve identity through the shared fail-closed gate and pass its returned key
to each gateway request; never inherit a parent session's authority.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import mcp_core
from kiro_crew.validation import (
    SESSION_CHANNEL_MANAGE_SCHEMA,
    SESSION_CHANNEL_POST_SCHEMA,
    SESSION_CHECKPOINT_SCHEMA,
    SESSION_RESTART_CONTINUATION_SCHEMA,
    validate_tool_args,
)


def session_checkpoint(name: str, args: dict[str, Any]) -> str:
    """Persist a checkpoint through the caller-bound gateway endpoint."""
    args = validate_tool_args(args, SESSION_CHECKPOINT_SCHEMA)
    session_key, error = mcp_core.require_strict_session_key(
        "Error: session_checkpoint requires a verified dashboard session identity."
    )
    if not session_key:
        return error
    result = mcp_core._post("/api/session-checkpoint", args, session_key=session_key)
    if result.get("ok") is not True:
        if result.get("transport_error") is True:
            return (
                "Checkpoint outcome is indeterminate because the gateway response was lost; "
                "do not retry automatically."
            )
        if result.get("code") == "checkpoint_persistence_failed":
            return (
                "Checkpoint was accepted and is pending durable persistence; "
                "do not retry automatically."
            )
        return f"Error: checkpoint was not recorded: {result.get('error', 'unknown error')}"
    calls = result.get("calls_this_turn")
    if isinstance(calls, int) and calls > 1:
        return (
            f"Checkpoint recorded (call {calls} this turn). One checkpoint per turn is enough: "
            "the card shows only the latest and the trail already kept the earlier milestone. "
            "Do not checkpoint again before the next user turn."
        )
    return "Checkpoint recorded for this session's Multiplex view."


def _session_channel_post(body: dict[str, Any]) -> dict[str, Any] | None:
    """Call the attached-channel endpoint with a verified session identity."""
    session_key, _error = mcp_core.require_strict_session_key(
        "Error: session channel access requires a verified session identity."
    )
    if not session_key:
        return None
    return mcp_core._post("/api/session-channel", body, session_key=session_key)


def session_channel_status(name: str, args: dict[str, Any]) -> str:
    if args:
        return "Error: session_channel_status takes no arguments."
    result = _session_channel_post({"action": "status"})
    if result is None:
        return "Error: session_channel_status requires a verified session identity."
    if result.get("ok") is not True:
        return f"Error: channel status unavailable: {result.get('error', 'unknown error')}"
    channels = result.get("channels") or []
    if not channels:
        return "No persistent-agent channel is attached to this session."
    summaries = []
    for channel in channels:
        peers = channel.get("peers") or []
        peer_text = ", ".join(
            f"{peer.get('role', 'Peer')} ({peer.get('id', 'unknown')}, {peer.get('state', 'unknown')})"
            for peer in peers
        )
        capacity = channel.get("capacity") or {}
        capacity_text = (
            f" Capacity: {capacity.get('used', 0)}/{capacity.get('max', 0)}."
            if isinstance(capacity, dict)
            else ""
        )
        own = channel.get("self") or {}
        self_text = (
            f" You: {own.get('role', 'member')} ({own.get('id', 'unknown')})"
            f"{', coordinator' if channel.get('coordinator') is True else ''}."
            if isinstance(own, dict) and own
            else ""
        )
        summaries.append(
            f"{channel.get('id', 'unknown')} — {channel.get('topic', '')}:{self_text} "
            f"Peers: {peer_text or 'none'}.{capacity_text}"
        )
    return "Attached persistent-agent channels:\n" + "\n".join(summaries)


def session_channel_post(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SESSION_CHANNEL_POST_SCHEMA)
    result = _session_channel_post({"action": "post", **args})
    if result is None:
        return "Error: session_channel_post requires a verified session identity."
    if result.get("ok") is True:
        message = result.get("message") or {}
        receipts = message.get("receipts") or []
        receipt_text = ", ".join(
            f"{item.get('recipient', 'unknown')}: {item.get('status', 'not_delivered')}"
            for item in receipts
            if isinstance(item, dict)
        )
        suffix = f" Delivery: {receipt_text}." if receipt_text else ""
        return f"Peer report {message.get('id', 'recorded')}.{suffix}"
    return f"Error: peer report was not recorded: {result.get('error', 'unknown error')}"


def session_channel_manage(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SESSION_CHANNEL_MANAGE_SCHEMA)
    result = _session_channel_post(args)
    if result is None:
        return "Error: session_channel_manage requires a verified session identity."
    if result.get("ok") is not True:
        return f"Error: channel membership was not changed: {result.get('error', 'unknown error')}"
    action = args["action"]
    if action == "add_agent":
        agent = result.get("agent") or {}
        return f"Channel agent added: {agent.get('role', 'Agent')} ({agent.get('id', 'unknown')})."
    if action == "request_attach":
        pending = result.get("request") or {}
        return (
            f"Attach request {pending.get('id', 'recorded')} filed for "
            f"{pending.get('title') or pending.get('slot', 'the session')}. Membership has not "
            "changed: the dashboard user decides in the channel UI, and the outcome arrives "
            "as a channel system message."
        )
    return f"Channel member removed: {result.get('removed_member_id', 'unknown')}."


def session_restart_continuation(name: str, args: dict[str, Any]) -> str:
    """Arm one persisted verification turn for the verified calling session."""
    args = validate_tool_args(args, SESSION_RESTART_CONTINUATION_SCHEMA)
    session_key, error = mcp_core.require_strict_session_key(
        "Error: session_restart_continuation requires a verified session identity."
    )
    if not session_key:
        return error
    result = mcp_core._post("/api/session-restart-continuation", args, session_key=session_key)
    if result.get("ok") is True:
        return "Post-restart verification is armed for this session."
    return (
        "Error: post-restart verification was not armed: " f"{result.get('error', 'unknown error')}"
    )


def _maintenance_post(action: str) -> dict[str, Any] | None:
    """Call the session-scoped maintenance endpoint with a verified identity."""
    session_key, _error = mcp_core.require_strict_session_key(
        "Error: session maintenance requires a verified session identity."
    )
    if not session_key:
        return None
    return mcp_core._post("/api/session-maintenance", {"action": action}, session_key=session_key)


def maintenance_status(name: str, args: dict[str, Any]) -> str:
    if args:
        return "Error: maintenance_status takes no arguments."
    result = _maintenance_post("status")
    if result is None:
        return "Error: maintenance_status requires a verified session identity."
    if result.get("ok") is not True:
        return f"Error: maintenance status unavailable: {result.get('error', 'unknown error')}"
    maintenance = result.get("maintenance") or {}
    if not maintenance.get("active"):
        return "No coordinated session reset is active."
    if maintenance.get("ready"):
        return "The coordinated reset barrier is ready; wait for the operator to restart sessions."
    return (
        "A coordinated reset is waiting. Pending dashboard sessions: "
        f"{', '.join(maintenance.get('pending') or []) or 'none'}; busy slotless workers: "
        f"{', '.join(maintenance.get('unmanaged_busy') or []) or 'none'}."
    )


def maintenance_acknowledge(name: str, args: dict[str, Any]) -> str:
    if args:
        return "Error: maintenance_acknowledge takes no arguments."
    result = _maintenance_post("acknowledge")
    if result is None:
        return "Error: maintenance_acknowledge requires a verified session identity."
    if result.get("ok") is not True:
        return (
            "Error: reset acknowledgement was not recorded: "
            f"{result.get('error', 'unknown error')}"
        )
    return "Reset acknowledgement recorded. Stop work and end this turn now."
