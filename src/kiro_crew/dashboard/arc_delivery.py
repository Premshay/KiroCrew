"""Deliver urgent arc board entries mid-turn through the arc's channel."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew import arc_brief, arcs
from kiro_crew.session_ledger import ledger_key

logger = logging.getLogger(__name__)


def _dedupe(keys: list[str]) -> list[str]:
    return list(dict.fromkeys(keys))


def urgent_recipients(arc_id: str, entry: dict[str, Any]) -> list[str]:
    """Member keys that must hear about ``entry`` now; the owner is never listed."""
    author = entry.get("author", "")
    kind = entry.get("kind")
    found: list[str] = []
    if kind == "FAIL":
        found += list(arcs.read_arc(arc_id)["members"])
    if kind == "ASK" and entry.get("to") not in (None, arcs.OWNER):
        found.append(entry["to"])
    if entry.get("reply_to"):
        for earlier in arcs.read_entries(arc_id, limit=10**9):
            if earlier["id"] == entry["reply_to"] and earlier["kind"] == "ASK":
                found.append(earlier["author"])
    if kind == "CLAIM":
        clashing = arcs.claim_collisions(arc_id, entry)
        if clashing:
            found += [c["author"] for c in clashing] + [author]
    return [k for k in _dedupe(found) if k != arcs.OWNER and (k != author or kind == "CLAIM")]


def _content(arc: dict[str, Any], entry: dict[str, Any]) -> str:
    author = entry["author"]
    role = arcs.OWNER if author == arcs.OWNER else arc["members"].get(author, {}).get("role")
    text = f"{entry['kind']} from {role or author}: {entry['text']}"
    if entry.get("link"):
        text += f" — {entry['link']}"
    return arc_brief.safe_line(text)


def _notify_owner(state: Any, arc: dict[str, Any], entry: dict[str, Any], content: str) -> dict:
    state.notify(
        "arc_ask",
        f"Arc question: {arc_brief.safe_line(arc['title'])}",
        content,
        meta={"arc_id": arc["id"], "entry_id": entry["id"]},
    )
    return {"recipients": [arcs.OWNER], "outcome": "posted"}


def _plan(arc_id: str, entry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The arc and the urgent recipients; takes the store lock, so it runs off the loop."""
    arc = arcs.read_arc(arc_id)
    if entry["kind"] == "ASK" and entry.get("to") == arcs.OWNER:
        return arc, []
    return arc, urgent_recipients(arc_id, entry)


async def deliver_entry(state: Any, arc_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Post an urgent entry to its recipients; never raises."""
    try:
        arc, keys = await asyncio.to_thread(_plan, arc_id, entry)
        content = _content(arc, entry)
        if entry["kind"] == "ASK" and entry.get("to") == arcs.OWNER:
            return _notify_owner(state, arc, entry, content)
        if not keys:
            return {"recipients": [], "outcome": "skipped"}
        channel = state.channel_manager.get(arc["channel_id"])
        if channel is None:
            raise LookupError("arc channel is not open")
        by_key = {ledger_key(m.session_key): mid for mid, m in channel.members.items()}
        reachable = [k for k in keys if k in by_key]
        unreachable = [k for k in keys if k not in by_key]
        # Channel.post never delivers to its own sender, so a recipient who is also the
        # sender cannot be reached: the colliding author posts as the orchestrator.
        sender = channel.orchestrator_id
        if sender is None or entry["author"] not in reachable:
            sender = by_key.get(entry["author"]) or sender
        if sender is None:
            raise LookupError("arc channel has no sender")
        for key in [k for k in reachable if by_key[k] == sender]:
            reachable.remove(key)
            unreachable.append(key)
        result: dict[str, Any] = {"recipients": reachable, "outcome": "skipped"}
        if unreachable:
            result["unreachable"] = unreachable
        if not reachable:
            return result
        await channel.post(
            sender,
            content,
            from_role=arc_brief._author_label(arc, entry["author"]),
            mention=[by_key[k] for k in reachable],
            msg_type="mention",
            delivery="interrupt",
            meta={"arc_id": arc["id"], "entry_id": entry["id"]},
        )
        result["outcome"] = "posted"
        return result
    except Exception:
        logger.warning("arc delivery failed", exc_info=True)
        return {"recipients": [], "outcome": "failed"}
