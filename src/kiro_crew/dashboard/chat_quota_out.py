"""A dashboard chat turn that failed on its vendor's quota marks that vendor out.

:func:`mark_quota_out` is called where a turn's provider failure becomes the
chat's error (both refusal branches of ``segment_flush`` and the two error arms
of ``turn_failures``). It writes the same ``vendor_limits`` mark a spawned run
writes (``quota_switch.note_chat_quota``), so ``spawn.route`` avoids that vendor
(Antigravity: that model group) until the reset. It never switches the chat's
model, never raises and never delays the error: a cheap regex gate on the event
loop, the mark itself on a worker thread nobody awaits.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Only the tail of a streamed reply is read: the provider fault is the last thing it streamed.
_TEXT_TAIL_CHARS = 600

#: Marks in flight, held so the loop does not drop a future nobody awaits.
_PENDING: set[asyncio.Future[Any]] = set()


def _turn_model(turn: Any) -> str:
    model = str(getattr(turn, "_turn_model", "") or "")
    if model and model.lower() != "auto":
        return model
    try:
        from kiro_crew.llm_helpers import provider_active_model

        return provider_active_model(getattr(turn, "client", None))
    except Exception:
        return ""


def mark_quota_out(turn: Any, *texts: object, refusal: bool = False) -> asyncio.Future[Any] | None:
    """Schedule the vendor's quota out-mark when *texts* carry a quota signal. Never raises.

    *texts* are errors (``str()`` is taken here); *refusal* adds the refused
    turn's streamed reply and explanation (:func:`refusal_texts`). Returns the
    scheduled future (tests await it), ``None`` when nothing was scheduled.
    """
    try:
        from kiro_crew.subagent_manager.quota_switch import QUOTA_PATTERNS, note_chat_quota

        if refusal:
            texts = (*texts, *refusal_texts(turn))
        text = "\n".join(str(t)[-_TEXT_TAIL_CHARS:] for t in texts if t)
        if not text or not any(rx.search(text) for rxs in QUOTA_PATTERNS.values() for rx in rxs):
            return None
        call = functools.partial(
            note_chat_quota,
            text,
            runtime_agent=str(getattr(turn, "kiro_agent", "") or ""),
            agent=str(getattr(turn, "owner_agent", "") or ""),
            provider=str(getattr(turn, "provider_name", "") or ""),
            model=_turn_model(turn),
        )
        fut = asyncio.get_running_loop().run_in_executor(None, call)
        _PENDING.add(fut)
        fut.add_done_callback(_PENDING.discard)
        return fut
    except Exception:
        logger.debug("Chat quota out-mark not scheduled", exc_info=True)
        return None


def refusal_texts(turn: Any) -> tuple[str, ...]:
    """What a refused turn said: its streamed reply and the refusal's own explanation."""
    refusal = getattr(turn, "_turn_refusal", None)
    return (
        str(getattr(turn, "assistant_text", "") or ""),
        str(getattr(refusal, "explanation", "") or ""),
    )
