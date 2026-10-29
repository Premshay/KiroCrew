"""Claude background-turn rendering and durable usage."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        ClaudeAutonomousTurn,
        DashboardState,
        LLMEvent,
        _ChatSlot,
        _flush_segment,
        _redact_tool_field,
        _schedule_widget_registration,
        _tool_call_ws_payload,
        _tool_meta,
        append_and_surface,
        hashlib,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        save_slot_off_loop,
    )


async def _render_claude_idle_event(
    state: DashboardState,
    slot: _ChatSlot,
    event: "LLMEvent",
) -> None:
    """Persist and broadcast one row of between-turn work without turn bookkeeping."""
    try:
        if event.kind == EVENT_TOOL_CALL:
            payload = _tool_call_ws_payload(event)
            payload["slot"] = slot.key
            state.broadcast_ws("tool_call", payload)
            append_and_surface(
                state,
                slot,
                "tool",
                f"🔧 {payload['tool']}",
                "msg msg-tool",
                meta={**(_tool_meta(event) or {}), "between_turn": True},
            )
        elif event.kind == EVENT_TOOL_RESULT:
            tool_call_id = _redact_tool_field(event.tool_call_id) if event.tool_call_id else ""
            if not tool_call_id:
                return
            output = _redact_tool_field(event.tool_output)
            output, _ = redact_exfiltration_urls(output)
            output, _ = redact_credentials(output)
            for message in slot.messages:
                if (
                    message.get("role") == "tool"
                    and message.get("meta", {}).get("tool_call_id") == tool_call_id
                ):
                    meta = message.setdefault("meta", {})
                    meta["done"] = True
                    if output or "output" not in meta:
                        meta["output"] = output
            state.broadcast_ws(
                "tool_result",
                {"slot": slot.key, "tool_call_id": tool_call_id, "output": output},
            )
        elif event.kind == EVENT_TEXT_CHUNK:
            text = (event.text or "").strip()
            if not text:
                return
            text, exfil_warnings = redact_exfiltration_urls(text)
            for warning in exfil_warnings:
                logger.warning("Exfiltration URL redacted in idle Claude output: %s", warning)
            text, credential_warnings = redact_credentials(text)
            for warning in credential_warnings:
                logger.warning("Credential redacted in idle Claude output: %s", warning)
            if event.message_id:
                slot.segment_message_id = event.message_id
            _flush_segment(
                state,
                slot,
                text,
                message_meta={"between_turn": True},
                surface_when_reader=True,
            )
        else:
            return
        await save_slot_off_loop(state, slot, best_effort=False)
        state.push_slots_update()
    except Exception:
        logger.warning("idle row render failed for slot %s", slot.key, exc_info=True)


def _autonomous_owner_is_slot(session_key: str, slot_key: str) -> bool:
    """Return whether a native Claude turn belongs to this dashboard slot."""
    if not session_key or session_key == slot_key:
        return True
    return any(
        session_key.startswith(prefix) and session_key[len(prefix) :] == slot_key
        for prefix in ("dashboard:", "dashboard_")
    )


async def _persist_claude_autonomous_turn(
    state: DashboardState,
    slot: _ChatSlot,
    turn: ClaudeAutonomousTurn,
) -> None:
    """Persist one native Claude background reply as its own assistant row."""
    if not _autonomous_owner_is_slot(turn.session_key, slot.key):
        logger.error(
            "Rejected Claude autonomous turn for another dashboard slot "
            "(turn_owner=%s, target_slot=%s, origin=%s)",
            turn.session_key,
            slot.key,
            turn.origin,
        )
        return
    if not turn.text.strip():
        return
    if turn.message_id:
        source_mid = f"m-claude-{turn.message_id}"
    else:
        fallback = f"{turn.origin}\x00{turn.timestamp}\x00{turn.text}".encode()
        source_mid = f"m-claude-fallback-{hashlib.sha256(fallback).hexdigest()[:24]}"
        logger.error("Claude autonomous turn arrived without a native message id")

    existing = any(
        message.get("role") == "assistant"
        and isinstance(message.get("meta"), dict)
        and message["meta"].get("mid") == source_mid
        for message in slot.messages
    )
    if existing:
        await save_slot_off_loop(state, slot, best_effort=False)
        return

    redacted, exfil_warnings = redact_exfiltration_urls(turn.text)
    for warning in exfil_warnings:
        logger.warning("Exfiltration URL redacted in autonomous Claude turn: %s", warning)
    redacted, credential_warnings = redact_credentials(redacted)
    for warning in credential_warnings:
        logger.warning("Credential redacted in autonomous Claude turn: %s", warning)

    message = slot.append(
        "assistant",
        redacted,
        "msg msg-a",
        meta={"mid": source_mid},
    )
    await save_slot_off_loop(state, slot, best_effort=False)
    _schedule_widget_registration(state, slot, redacted, str(message.get("ts", "")))
