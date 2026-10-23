"""Bounded recovery for provider context limits and interrupted streams."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _CONTEXT_WINDOW_RECOVERY_PREFIX,
        SYNTHETIC_RECOVERY_KIND,
        AcpConversationPayloadExceeded,
        _persist_partial_reply,
        _queue_recovery,
        _should_suppress_requeue,
        logger,
        payload_for_replay,
    )


async def _on_context_limit(turn, exc) -> None:
    slot = turn.slot
    payload_limit = isinstance(exc, AcpConversationPayloadExceeded)
    logger.warning(
        "%s exceeded in slot %s: %s",
        "Native attachment payload limit" if payload_limit else "Context window",
        slot.key,
        exc,
    )
    turn.needs_session_reset = True
    turn.needs_conversation_discard = True
    await _persist_partial_reply(turn, "error: context exceeded")
    if _should_suppress_requeue(slot) or turn._prompt_depth != 0:
        slot.append(
            "error",
            (
                "The provider attachment limit was reached. The session was rebuilt; please retry."
                if payload_limit
                else "The local model context is full. The session was rebuilt; please retry."
            ),
            "msg msg-err",
        )
    elif turn._context_window_recovery:
        slot.append(
            "error",
            (
                "The rebuilt provider conversation still exceeds its attachment limit. "
                "Remove older attachments, then retry."
                if payload_limit
                else "The rebuilt local-model context still does not fit this request. "
                "Shorten the request or reduce the tool output, then retry."
            ),
            "msg msg-err",
        )
    else:
        slot.append(
            "error",
            (
                "Rebuilding the provider conversation without retained attachments and retrying…"
                if payload_limit
                else "Rebuilding the local-model context and retrying…"
            ),
            "msg msg-err",
        )
        reason = (
            "The previous native session was discarded because its retained attachments "
            "exceeded the provider request limit."
            if payload_limit
            else "The previous native session was discarded because it exceeded "
            "the model context window."
        )
        _queue_recovery(
            turn,
            0,
            f"{_CONTEXT_WINDOW_RECOVERY_PREFIX}\n"
            f"{reason} Treat the following as the user's original request "
            f"and answer it normally:\n\n{turn.message}",
            kind=SYNTHETIC_RECOVERY_KIND,
        )


async def _on_provider_interruption(turn, exc) -> None:
    slot = turn.slot
    logger.warning(
        "Provider stream interrupted in slot %s (emitted=%s tools=%d): %s",
        slot.key,
        turn._turn_emitted,
        turn._turn_tool_calls,
        exc,
    )
    await _persist_partial_reply(turn, "error: provider stream interrupted")
    if turn._turn_emitted or turn._turn_tool_calls:
        slot.append(
            "error",
            "Antigravity interrupted after activity. It was not retried automatically; "
            "review the output before continuing.",
            "msg msg-err",
        )
    elif _should_suppress_requeue(slot) or turn._prompt_depth != 0 or turn._synthetic_payload:
        slot.append(
            "error", "Antigravity interrupted before it started. Please retry.", "msg msg-err"
        )
    else:
        turn.needs_session_reset = True
        turn.needs_conversation_discard = True
        slot.append(
            "error",
            "⟳ Antigravity interrupted before it started — rebuilding its session and retrying…",
            "msg msg-err",
        )
        _queue_recovery(
            turn,
            0,
            turn.message,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=payload_for_replay(turn._is_synthetic),
        )
