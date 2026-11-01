"""Owner routes for the arc board console.

The member side (``handlers/arcs.py``) resolves identity from the session-ledger
boundary and takes no author field. These are the dashboard owner's own console
routes: the gate is ``require_owner_dashboard_request``, writes act as the arc
store's ``owner`` principal, and agent-written text leaves redacted the same way
it does on the member routes, so a secret an agent posted on the board never
reaches a screen unredacted.

``/api/arc-board`` is a deliberate prefix of its own: ``/api/arcs`` sits in
``_STRICT_INTERNAL_API_PATHS`` (loopback + internal secret, the members' door),
and these routes are reached from the owner's browser with the dashboard
identity the token middleware installs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from aiohttp import web

from kiro_crew import arc_brief, arc_metrics, arcs
from kiro_crew.dashboard.arc_delivery import deliver_entry
from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
from kiro_crew.dashboard.handlers._shared_request_body import read_bounded_json
from kiro_crew.dashboard.handlers.arcs import (
    _error_response,
)
from kiro_crew.dashboard.handlers.arcs import _public as _redacted

logger = logging.getLogger(__name__)


async def _run(operation: str, work: Callable[[], Any]) -> web.Response:
    try:
        payload = await asyncio.to_thread(work)
    except (arcs.ArcError, OSError) as exc:
        return _error_response(exc, operation)
    return web.json_response(payload)


async def _gate_body(
    request: web.Request, operation: str
) -> tuple[dict[str, Any] | None, web.Response | None]:
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return None, denied
    body, err = await read_bounded_json(request)
    if err is not None:
        return None, err
    return body or {}, None


def _invalid(message: str) -> web.Response:
    return web.json_response({"error": message, "code": "arc_validation"}, status=400)


def _arc_summary(arc: dict[str, Any]) -> dict[str, Any]:
    """One arc as the left rail shows it; owner-authored fields stay as written."""
    milestones = arc["milestones"]
    index = arc["milestone_index"]
    return {
        "id": arc["id"],
        "title": arc["title"],
        "goal": arc["goal"],
        "reason": arc["reason"],
        "lineage": arc["lineage"],
        "autonomy": arc["autonomy"],
        "milestone_index": index,
        "milestone": milestones[index]["name"] if index < len(milestones) else "",
        "milestones_total": len(milestones),
        "members": len(arc["members"]),
        "open_asks": len(arcs.open_asks(arc["id"])),
        "open_claims": len(arcs.open_claims(arc["id"])),
        "created_at": arc["created_at"],
    }


def _lanes(
    arc: dict[str, Any], entries: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each member as the console's lane list shows it, in join order."""
    out = []
    for key, member in sorted(arc["members"].items(), key=lambda kv: kv[1]["joined_at"]):
        mine = [e for e in entries if e["author"] == key]
        claim = next((c for c in reversed(claims) if c["author"] == key), None)
        last = mine[-1] if mine else None
        out.append(
            {
                "key": key,
                "role": member["role"],
                "joined_at": member["joined_at"],
                "entries": len(mine),
                "last_ts": last["ts"] if last else None,
                "last_kind": last["kind"] if last else None,
                "claim": _redacted(claim) if claim else None,
            }
        )
    return out


def _collisions(claims: list[dict[str, Any]]) -> list[list[str]]:
    """Groups of open claims by different lanes on one milestone and step."""
    groups: list[list[str]] = []
    seen: set[str] = set()
    for claim in claims:
        if claim["id"] in seen:
            continue
        clash = arcs.collides(claims, claim)
        if clash:
            group = [claim["id"], *(c["id"] for c in clash)]
            seen.update(group)
            groups.append(group)
    return groups


async def api_arc_board_list(request: web.Request) -> web.Response:
    """GET /api/arc-board/arcs — every arc, oldest first, with its open counts."""
    denied = await require_owner_dashboard_request(request, "arc_board.list")
    if denied is not None:
        return denied
    return await _run(
        "arc_board.list",
        lambda: {"arcs": [_arc_summary(arc) for arc in arcs.list_arcs()]},
    )


async def api_arc_board_detail(request: web.Request) -> web.Response:
    """GET /api/arc-board/arcs/{arc_id} — the arc, its lanes, entries, briefs and open items."""
    denied = await require_owner_dashboard_request(request, "arc_board.detail")
    if denied is not None:
        return denied
    arc_id = request.match_info.get("arc_id", "")

    def work() -> dict[str, Any]:
        arc = arcs.read_arc(arc_id)
        entries = arcs.read_entries(arc_id, limit=10**9)
        claims = arcs.open_claims(arc_id)
        index = arc["milestone_index"]
        return {
            "arc": arc,
            "lanes": _lanes(arc, entries, claims),
            "briefs": {key: arc_brief.brief_for(arc, key) for key in arc["members"]},
            "entries": [_redacted(e) for e in entries],
            "open_claims": [_redacted(c) for c in claims],
            "open_asks": [
                {**_redacted(a), "seen_by": arcs.seen_by(arc_id, a["id"])}
                for a in arcs.open_asks(arc_id)
            ],
            "collisions": _collisions(claims),
            "milestone_reports": [
                _redacted(e) for e in entries if e["kind"] == "DONE" and e["milestone"] == index
            ],
        }

    return await _run("arc_board.detail", work)


async def api_arc_board_report(request: web.Request) -> web.Response:
    """GET /api/arc-board/arcs/{arc_id}/report — the board-only pilot metrics."""
    denied = await require_owner_dashboard_request(request, "arc_board.report")
    if denied is not None:
        return denied
    arc_id = request.match_info.get("arc_id", "")
    return await _run(
        "arc_board.report",
        lambda: arc_metrics.board_report(arc_id, datetime.now(timezone.utc)),
    )


async def api_arc_board_create(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs — create an arc and its first members in one write."""
    body, err = await _gate_body(request, "arc_board.create")
    if err is not None:
        return err
    assert body is not None
    spec = dict(body)
    members = spec.pop("members", None) or []
    return await _run("arc_board.create", lambda: {"arc": arcs.create_arc(spec, members=members)})


async def api_arc_board_add_member(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/members — add (or re-role) a lane."""
    body, err = await _gate_body(request, "arc_board.add_member")
    if err is not None:
        return err
    assert body is not None
    arc_id = request.match_info.get("arc_id", "")
    session_key, role = body.get("session_key"), body.get("role")
    if not isinstance(session_key, str) or not isinstance(role, str):
        return _invalid("session_key and role are required")
    return await _run(
        "arc_board.add_member",
        lambda: {"arc": arcs.add_member(arc_id, session_key, role)},
    )


async def api_arc_board_remove_member(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/members/remove — take a lane off the arc."""
    body, err = await _gate_body(request, "arc_board.remove_member")
    if err is not None:
        return err
    assert body is not None
    arc_id = request.match_info.get("arc_id", "")
    session_key = body.get("session_key")
    if not isinstance(session_key, str):
        return _invalid("session_key is required")
    return await _run(
        "arc_board.remove_member",
        lambda: {"arc": arcs.remove_member(arc_id, session_key)},
    )


async def api_arc_board_autonomy(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/autonomy — change the arc's autonomy level."""
    body, err = await _gate_body(request, "arc_board.autonomy")
    if err is not None:
        return err
    assert body is not None
    arc_id = request.match_info.get("arc_id", "")
    return await _run(
        "arc_board.autonomy",
        lambda: {"arc": arcs.set_autonomy(arc_id, body.get("autonomy"))},
    )


async def _owner_post(
    request: web.Request,
    operation: str,
    arc_id: str,
    build: Callable[[], dict[str, Any]],
) -> web.Response:
    """Append an owner entry, then deliver it by the board's urgency rules."""
    try:
        entry = await asyncio.to_thread(lambda: arcs.append_entry(arc_id, arcs.OWNER, build()))
    except (arcs.ArcError, OSError) as exc:
        return _error_response(exc, operation)
    try:
        delivery = await deliver_entry(request.app.get("state"), arc_id, entry)
    except Exception:
        logger.warning("arc %s delivery failed for %s", operation, arc_id, exc_info=True)
        delivery = {"recipients": [], "outcome": "failed"}
    return web.json_response({"entry": _redacted(entry), "delivery": delivery})


async def api_arc_board_answer(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/answer — answer an open ask as the owner.

    The answer is a FACT replying to the ask, exactly the CLI's ``kirocrew arc
    answer``; delivery then carries it to the lane that asked.
    """
    body, err = await _gate_body(request, "arc_board.answer")
    if err is not None:
        return err
    assert body is not None
    arc_id = request.match_info.get("arc_id", "")
    ask_id, text = body.get("ask_id"), body.get("text")
    if not isinstance(ask_id, str) or not isinstance(text, str):
        return _invalid("ask_id and text are required")

    def build() -> dict[str, Any]:
        entries = arcs.read_entries(arc_id, limit=10**9)
        if not any(e["id"] == ask_id and e["kind"] == "ASK" for e in entries):
            raise arcs.ArcError(f"no ASK {ask_id!r} in arc {arc_id}")
        return {"kind": "FACT", "text": text, "reply_to": ask_id}

    return await _owner_post(request, "arc_board.answer", arc_id, build)


async def api_arc_board_note(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/note — an owner FACT to every lane.

    Used to send a reported milestone back with a note instead of accepting it.
    """
    body, err = await _gate_body(request, "arc_board.note")
    if err is not None:
        return err
    assert body is not None
    arc_id = request.match_info.get("arc_id", "")
    text = body.get("text")
    if not isinstance(text, str):
        return _invalid("text is required")
    return await _owner_post(
        request, "arc_board.note", arc_id, lambda: {"kind": "FACT", "text": text}
    )


async def api_arc_board_accept(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/accept — accept the milestone the owner was shown.

    ``expected_index`` is the milestone the console displayed; the store checks it
    under the arc lock, so a double click or a CLI accept in between answers 409
    instead of skipping a milestone.
    """
    body, err = await _gate_body(request, "arc_board.accept")
    if err is not None:
        return err
    assert body is not None
    arc_id = request.match_info.get("arc_id", "")
    expected = body.get("expected_index")
    if not isinstance(expected, int) or isinstance(expected, bool):
        return _invalid("expected_index is required")
    return await _run("arc_board.accept", lambda: {"arc": arcs.accept_milestone(arc_id, expected)})
