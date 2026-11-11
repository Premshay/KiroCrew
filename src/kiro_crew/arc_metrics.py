"""Board-derived coordination metrics for one arc.

Shared by the owner console's Report tab (``GET /api/arc-board/arcs/{id}/report``)
and ``scripts/arc_pilot_report.py``. Everything here reads only the arc store;
PR-derived product guards need ``gh`` and stay in the script.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime
from typing import Any

from kiro_crew import arcs


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _minutes(delta_secs: float) -> float:
    return round(delta_secs / 60, 1)


def _median_max(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"median": None, "max": None}
    return {"median": round(statistics.median(values), 1), "max": round(max(values), 1)}


def window_entries(arc_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    entries = arcs.read_entries(arc_id, limit=10**9)
    return [e for e in entries if since <= _ts(e["ts"]) < until]


def collision_count(entries: list[dict[str, Any]]) -> int:
    """Claims that, when written, matched another author's still-open claim."""
    open_claims: list[dict[str, Any]] = []
    count = 0
    for entry in entries:
        if entry["kind"] == "CLAIM":
            if arcs.collides(open_claims, entry):
                count += 1
            open_claims.append(entry)
        elif entry.get("reply_to"):
            open_claims = [c for c in open_claims if not arcs.claim_closed_by(c, entry)]
    return count


def repeated_fails(entries: list[dict[str, Any]]) -> int:
    """FAIL entries whose text an earlier FAIL by a different author already recorded."""
    seen: dict[str, set[str]] = {}
    count = 0
    for entry in entries:
        if entry["kind"] != "FAIL":
            continue
        authors = seen.setdefault(" ".join(entry["text"].casefold().split()), set())
        if authors - {entry["author"]}:
            count += 1
        authors.add(entry["author"])
    return count


def idle_gaps(entries: list[dict[str, Any]], until: datetime) -> dict[str, Any]:
    closed: list[float] = []
    per_member: dict[str, list[float]] = {}
    open_gaps: dict[str, float] = {}
    members = sorted({e["author"] for e in entries if e["author"] not in (arcs.OWNER, arcs.GITHUB)})
    for member in members:
        mine = [e for e in entries if e["author"] == member]
        for i, entry in enumerate(mine):
            if entry["kind"] not in ("DONE", "FAIL"):
                continue
            nxt = next((e for e in mine[i + 1 :] if e["kind"] == "CLAIM"), None)
            if nxt is not None:
                gap = _minutes((_ts(nxt["ts"]) - _ts(entry["ts"])).total_seconds())
                per_member.setdefault(member, []).append(gap)
                closed.append(gap)
            else:
                gap = _minutes((until - _ts(entry["ts"])).total_seconds())
                open_gaps[member] = max(open_gaps.get(member, 0.0), gap)
    return {
        "overall": _median_max(closed),
        "per_member": {m: _median_max(v) for m, v in per_member.items()},
        "open_gaps_min": open_gaps,
    }


def ask_ages(arc_id: str, entries: list[dict[str, Any]], until: datetime) -> dict[str, Any]:
    first_reply: dict[str, datetime] = {}
    for entry in entries:
        if entry.get("reply_to"):
            first_reply.setdefault(entry["reply_to"], _ts(entry["ts"]))
    asks = [e for e in entries if e["kind"] == "ASK"]
    replied = [
        _minutes((first_reply[a["id"]] - _ts(a["ts"])).total_seconds())
        for a in asks
        if a["id"] in first_reply
    ]
    window_ids = {a["id"] for a in asks}
    open_ids = {a["id"]: a for a in arcs.open_asks(arc_id) if a["id"] in window_ids}
    open_ages = [_minutes((until - _ts(a["ts"])).total_seconds()) for a in open_ids.values()]
    return {
        "time_to_reply_min": _median_max(replied),
        "open_asks": len(open_ids),
        "oldest_open_ask_min": max(open_ages) if open_ages else None,
    }


def reading_load(entries: list[dict[str, Any]]) -> int:
    return sum(
        len(e["text"].split())
        for e in entries
        if e["kind"] == "ASK" and e.get("to") == arcs.OWNER and e["author"] != arcs.OWNER
    )


def board_report(arc_id: str, until: datetime) -> dict[str, Any]:
    """Every board-only metric for the whole arc, as of ``until``."""
    entries = arcs.read_entries(arc_id, limit=10**9)
    by_lane: dict[str, Counter[str]] = {}
    for entry in entries:
        by_lane.setdefault(entry["author"], Counter())[entry["kind"]] += 1
    return {
        "as_of": until.isoformat(timespec="seconds"),
        "since": entries[0]["ts"] if entries else None,
        "entries": len(entries),
        "by_kind": dict(Counter(e["kind"] for e in entries)),
        "by_lane": {lane: dict(kinds) for lane, kinds in by_lane.items()},
        "done_links": [e["link"] for e in entries if e["kind"] == "DONE" and e.get("link")],
        # Merged arc PRs recorded from GitHub, and those a reviewer lane cleared
        # on their exact final head.
        "merged_prs": [e["pr"] for e in entries if e["author"] == arcs.GITHUB],
        "verified_prs": [
            e["pr"] for e in entries if e["author"] == arcs.GITHUB and e.get("verified")
        ],
        "idle_gaps": idle_gaps(entries, until),
        "ask_age": ask_ages(arc_id, entries, until),
        "collisions": collision_count(entries),
        "repeated_fails": repeated_fails(entries),
        "reading_load_words": reading_load(entries),
    }
