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
from typing import Any, Callable

from aiohttp import web

from kiro_crew import arc_brief, arcs
from kiro_crew.dashboard.arc_delivery import deliver_entry
from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
from kiro_crew.dashboard.handlers._shared_request_body import read_bounded_json
from kiro_crew.dashboard.handlers.arcs import (
    _error_response,
    _public as _redacted,
)

logger = logging.getLogger(__name__)


async def _run(request: web.Request, operation: str, work: Callable[[], Any]) -> web.Response:
    try:
        payload = await asyncio.to_thread(work)
    except (arcs.ArcError, OSError) as exc:
        return _error_response(exc, operation)
    return web.json_response(payload)


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


async def api_arc_board_list(request: web.Request) -> web.Response:
    """GET /api/arc-board/arcs — every arc, oldest first, with its open counts."""
    denied = await require_owner_dashboard_request(request, "arc_board.list")
    if denied is not None:
        return denied

    def work() -> dict[str, Any]:
        return {"arcs": [_arc_summary(arc) for arc in arcs.list_arcs()]}

    return await _run(request, "arc_board.list", work)


async def api_arc_board_detail(request: web.Request) -> web.Response:
    """GET /api/arc-board/arcs/{arc_id} — the arc, its entries, brief and open items."""
    denied = await require_owner_dashboard_request(request, "arc_board.detail")
    if denied is not None:
        return denied
    arc_id = request.match_info.get("arc_id", "")

    def work() -> dict[str, Any]:
        arc = arcs.read_arc(arc_id)
        asks = arcs.open_asks(arc_id)
        return {
            "arc": arc,
            "brief": arc_brief.brief_for(arc, arcs.OWNER),
            "entries": [_redacted(e) for e in arcs.read_entries(arc_id, limit=10**9)],
            "open_claims": [_redacted(c) for c in arcs.open_claims(arc_id)],
            "open_asks": [
                {**_redacted(a), "seen_by": arcs.seen_by(arc_id, a["id"])} for a in asks
            ],
        }

    return await _run(request, "arc_board.detail", work)


async def api_arc_board_create(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs — create an arc (and its first members) as the owner."""
    denied = await require_owner_dashboard_request(request, "arc_board.create")
    if denied is not None:
        return denied
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    assert body is not None
    spec = dict(body)
    members = spec.pop("members", None) or []
    if not isinstance(members, list):
        return web.json_response(
            {"error": "members must be a list", "code": "arc_validation"}, status=400
        )

    def work() -> dict[str, Any]:
        arc = arcs.create_arc(spec)
        for member in members:
            if not isinstance(member, dict):
                raise arcs.ArcError("each member needs a session_key and a role")
            # add_member reloads the arc; carry the freshest copy through.
            arc = arcs.add_member(
                arc["id"], str(member.get("session_key") or ""), str(member.get("role") or "")
            )
        return {"arc": arc}

    return await _run(request, "arc_board.create", work)


async def api_arc_board_add_member(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/members — add (or re-role) a lane to the arc."""
    denied = await require_owner_dashboard_request(request, "arc_board.add_member")
    if denied is not None:
        return denied
    arc_id = request.match_info.get("arc_id", "")
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    assert body is not None
    session_key = body.get("session_key")
    role = body.get("role")
    if not isinstance(session_key, str) or not isinstance(role, str):
        return web.json_response(
            {"error": "session_key and role are required", "code": "arc_validation"}, status=400
        )

    def work() -> dict[str, Any]:
        return {"arc": arcs.add_member(arc_id, session_key, role)}

    return await _run(request, "arc_board.add_member", work)


async def api_arc_board_answer(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/answer — answer an open ask as the owner.

    The answer is a FACT replying to the ask, exactly the CLI's ``kirocrew arc
    answer``; delivery then carries it to the lane that asked.
    """
    denied = await require_owner_dashboard_request(request, "arc_board.answer")
    if denied is not None:
        return denied
    arc_id = request.match_info.get("arc_id", "")
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    assert body is not None
    ask_id = body.get("ask_id")
    text = body.get("text")
    if not isinstance(ask_id, str) or not isinstance(text, str):
        return web.json_response(
            {"error": "ask_id and text are required", "code": "arc_validation"}, status=400
        )

    def work() -> dict[str, Any]:
        asks = [e for e in arcs.read_entries(arc_id, limit=10**9) if e["kind"] == "ASK"]
        if not any(e["id"] == ask_id for e in asks):
            raise arcs.ArcError(f"no ASK {ask_id!r} in arc {arc_id}")
        return arcs.append_entry(
            arc_id, arcs.OWNER, {"kind": "FACT", "text": text, "reply_to": ask_id}
        )

    try:
        entry = await asyncio.to_thread(work)
    except (arcs.ArcError, OSError) as exc:
        return _error_response(exc, "arc_board.answer")
    try:
        delivery = await deliver_entry(request.app.get("state"), arc_id, entry)
    except Exception:
        logger.warning("arc answer delivery failed for %s", arc_id, exc_info=True)
        delivery = {"recipients": [], "outcome": "failed"}
    return web.json_response({"entry": _redacted(entry), "delivery": delivery})


async def api_arc_board_accept(request: web.Request) -> web.Response:
    """POST /api/arc-board/arcs/{arc_id}/accept — the owner accepts the current milestone."""
    denied = await require_owner_dashboard_request(request, "arc_board.accept")
    if denied is not None:
        return denied
    arc_id = request.match_info.get("arc_id", "")

    try:
        arc = await asyncio.to_thread(arcs.read_arc, arc_id)
    except (arcs.ArcError, OSError) as exc:
        return _error_response(exc, "arc_board.accept")
    if arc["milestone_index"] >= len(arc["milestones"]):
        return web.json_response(
            {"error": "every milestone is already accepted", "code": "arc_complete"}, status=409
        )

    def work() -> dict[str, Any]:
        return {"arc": arcs.accept_milestone(arc_id)}

    return await _run(request, "arc_board.accept", work)
