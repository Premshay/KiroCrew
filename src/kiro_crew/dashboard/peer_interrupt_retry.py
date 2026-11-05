"""Retry a held peer interrupt at the next tool boundary.

A claude session holds a mid-turn steer while one of the turn's tool calls may
be open (``client_turn_control._steer_via_steering_request``): steering then
would make claude background the tool and end the turn early. The channel
delivery path (``handlers_channel.deliver_attached_channel_message``) therefore
puts a held interrupt first in the session's queue, where it runs when the turn
ends -- which, for a turn of many long tool calls, can be far later than the
sender meant by ``interrupt``.

This module remembers each held interrupt on the slot and retries the steer
after every tool result of the running turn. A steer the client now accepts
reaches the turn mid-flight, exactly as an unheld one would; its queued copy is
dropped so it does not run a second time. A steer still held stays remembered
for the next tool result, and the queued copy stays the fallback: if the turn
ends first, the queue runs it next, as before this module existed.

Every step is defensive: a failure here never disturbs the running turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.agent_sdk import is_acp_error

logger = logging.getLogger(__name__)

#: A held interrupt the slot still owes a retry.
_ATTR = "_held_peer_interrupts"


@dataclass
class HeldInterrupt:
    queue_id: str
    channel_id: str
    message_id: str
    member_id: str
    text: str


def remember_held_interrupt(slot: Any, held: HeldInterrupt) -> None:
    """Record a held interrupt so the next tool boundary retries its steer."""
    pending = getattr(slot, _ATTR, None)
    if pending is None:
        pending = []
        setattr(slot, _ATTR, pending)
    pending.append(held)


def held_interrupts(slot: Any) -> list[HeldInterrupt]:
    return list(getattr(slot, _ATTR, None) or [])


def _still_queued(slot: Any, queue_id: str) -> bool:
    return any(item.get("id") == queue_id for item in getattr(slot, "_queue", []) or [])


def _mark_receipt(state: Any, held: HeldInterrupt) -> None:
    """Upgrade the sender's receipt from ``held`` to ``steered``."""
    manager = getattr(state, "channel_manager", None)
    channel = manager.get(held.channel_id) if manager is not None else None
    message = getattr(channel, "_msg_index", {}).get(held.message_id) if channel else None
    if message is None:
        return
    message.delivery_status[held.member_id] = "steered"
    save = getattr(channel, "_save", None)
    if callable(save):
        save()


async def retry_held_peer_interrupts(state: Any, slot: Any) -> int:
    """Try each held interrupt's steer again; return how many reached the turn.

    Called after a tool result. A record whose queued copy is gone (the turn
    ended and the queue ran it, or the owner removed it) is dropped without a
    steer, so nothing is delivered twice.
    """
    pending = getattr(slot, _ATTR, None)
    if not pending:
        return 0
    client = getattr(slot, "_acp_client", None)
    if client is None or not getattr(client, "supports_steer", False):
        return 0
    if not getattr(slot, "turn_running", False):
        return 0
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.state import PEER_CHANNEL_REQUEST_KIND

    delivered = 0
    for held in list(pending):
        if not _still_queued(slot, held.queue_id):
            pending.remove(held)
            continue
        slot._pending_steers.append(held.text)
        try:
            steered = await client.steer(held.text)
        except Exception as exc:
            if not isinstance(exc, OSError) and not is_acp_error(exc):
                raise
            logger.warning("held peer interrupt retry failed for slot %s", slot.key, exc_info=True)
            steered = False
        if not steered:
            try:
                slot._pending_steers.remove(held.text)
            except ValueError:
                pass
            # Still held (a tool call opened again): keep it for the next boundary.
            break
        pending.remove(held)
        slot.queue_remove_by_id(held.queue_id)
        slot.append("inject", held.text, json.dumps({"kind": PEER_CHANNEL_REQUEST_KIND}))
        slot.remove_peer_channel_message(held.channel_id, held.message_id)
        _mark_receipt(state, held)
        logger.info(
            "held peer interrupt %s reached slot %s at a tool boundary", held.message_id, slot.key
        )
        delivered += 1
    if delivered:
        await save_slot_off_loop(state, slot, force=True, best_effort=False)
        state.push_slots_update()
    return delivered


async def retry_after_tool_result(state: Any, slot: Any) -> None:
    """The turn loop's hook: never lets a retry failure reach the running turn."""
    try:
        await retry_held_peer_interrupts(state, slot)
    except Exception:
        logger.warning("held peer interrupt retry raised for slot %s", slot.key, exc_info=True)
