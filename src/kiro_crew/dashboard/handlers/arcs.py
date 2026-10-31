"""Member routes for the durable per-arc board.

Identity comes only from the session-ledger boundary and no route takes a
target-session or author field, so a caller can read and write only the arcs it
is a member of, and only as itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew import arc_brief, arcs
from kiro_crew.dashboard.arc_delivery import deliver_entry
from kiro_crew.dashboard.handlers.session_ledger import _resolve_ledger_key
from kiro_crew.security.text_redaction import redact
from kiro_crew.validation import ARC_ENTRY_ROUTE_SCHEMA, ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

_ENTRY_TEXT_FIELDS = ("text", "step", "link")
_PAGE = 200


def _error_response(exc: Exception, operation: str) -> web.Response:
    if isinstance(exc, arcs.ArcNotFound):
        return web.json_response({"error": str(exc), "code": "arc_not_found"}, status=404)
    if isinstance(exc, arcs.ArcAccessDenied):
        logger.warning("arc access denied during %s", operation)
        return web.json_response({"error": str(exc), "code": "arc_access_denied"}, status=403)
    if isinstance(exc, arcs.ArcError):
        return web.json_response({"error": str(exc), "code": "arc_validation"}, status=400)
    if isinstance(exc, OSError):
        logger.warning("arc store unavailable during %s", operation, exc_info=True)
        return web.json_response(
            {"error": "arc store is busy; try again", "code": "arc_store_busy"}, status=503
        )
    raise exc


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry as a model may see it: every free-text field redacted."""
    out = dict(entry)
    for name in _ENTRY_TEXT_FIELDS:
        if isinstance(out.get(name), str):
            out[name] = redact(out[name])
    if isinstance(out.get("checked"), list):
        out["checked"] = [redact(str(c)) for c in out["checked"]]
    return out


def _member_arc(arc_id: str, key: str) -> dict[str, Any]:
    arc = arcs.read_arc(arc_id)
    if key not in arc["members"]:
        raise arcs.ArcAccessDenied(f"caller is not a member of arc {arc_id}")
    return arc


async def _run(request: web.Request, operation: str, work: Any) -> web.Response:
    key, refusal = await _resolve_ledger_key(request, operation)
    if refusal is not None:
        return refusal
    try:
        payload = await asyncio.to_thread(work, key)
    except (arcs.ArcError, OSError) as exc:
        return _error_response(exc, operation)
    return web.json_response(payload)


async def api_arcs_mine(request: web.Request) -> web.Response:
    """GET /api/arcs/mine — the arcs the caller belongs to."""

    def work(key: str) -> dict[str, Any]:
        found = []
        for arc in arcs.arcs_for_member(key):
            milestones = arc["milestones"]
            index = arc["milestone_index"]
            found.append(
                {
                    "id": arc["id"],
                    "title": arc["title"],
                    "goal": arc["goal"],
                    "milestone": milestones[index]["name"] if index < len(milestones) else "",
                }
            )
        return {"arcs": found}

    return await _run(request, "arc_mine", work)


async def api_arc_brief(request: web.Request) -> web.Response:
    """GET /api/arcs/{arc_id}/brief — the caller's standing brief."""
    arc_id = request.match_info.get("arc_id", "")

    def work(key: str) -> dict[str, Any]:
        return {"brief": arc_brief.brief_for(_member_arc(arc_id, key), key)}

    return await _run(request, "arc_brief", work)


async def api_arc_entries_read(request: web.Request) -> web.Response:
    """GET /api/arcs/{arc_id}/entries?after=&q= — read or grep; never moves the seen cursor."""
    arc_id = request.match_info.get("arc_id", "")
    query = request.query.get("q", "")
    after = request.query.get("after") or None

    def work(key: str) -> dict[str, Any]:
        _member_arc(arc_id, key)
        more = False
        if query:
            entries = arcs.grep_entries(arc_id, query)
        else:
            entries = arcs.read_entries(arc_id, after=after, limit=_PAGE + 1)
            more = len(entries) > _PAGE
            entries = entries[:_PAGE]
        payload: dict[str, Any] = {
            "entries": [_public(e) for e in entries],
            "open_asks": [_public(e) for e in arcs.open_asks(arc_id)],
        }
        if more:
            payload.update(truncated=True, next_after=entries[-1]["id"])
        return payload

    return await _run(request, "arc_entries_read", work)


def _urgent_unseen(arc_id: str, key: str) -> list[dict[str, Any]]:
    """Unseen peer entries the caller must not miss; the seen cursor is left to the turn prefix."""
    unseen = arcs.unseen_entries(arc_id, key)
    everything = arcs.read_entries(arc_id, limit=10**9)
    my_asks = {e["id"] for e in everything if e["kind"] == "ASK" and e["author"] == key}
    colliding: set[str] = set()
    for claim in arcs.open_claims(arc_id):
        if claim["author"] == key:
            colliding.update(c["id"] for c in arcs.claim_collisions(arc_id, claim))
    urgent = [
        e for e in unseen if e["kind"] == "FAIL" or e["reply_to"] in my_asks or e["id"] in colliding
    ]
    return urgent


async def api_arc_entries_write(request: web.Request) -> web.Response:
    """POST /api/arcs/{arc_id}/entries — append as the verified caller."""
    try:
        raw = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    try:
        body = validate_tool_args(raw, ARC_ENTRY_ROUTE_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "validation_error"}, status=400)
    arc_id = request.match_info.get("arc_id", "")

    def work(key: str) -> dict[str, Any]:
        _member_arc(arc_id, key)
        stored = arcs.append_entry(arc_id, key, body)
        return {
            "entry": _public(stored),
            "unseen": [_public(e) for e in _urgent_unseen(arc_id, key)],
        }

    response = await _run(request, "arc_entries_write", work)
    if response.status != 200:
        return response
    payload = json.loads(response.text)
    stored = payload["entry"]
    try:
        delivery = await deliver_entry(request.app.get("state"), arc_id, stored)
    except Exception:
        logger.warning("arc delivery failed for %s", arc_id, exc_info=True)
        delivery = {"recipients": [], "outcome": "failed"}
    payload["delivery"] = delivery
    return web.json_response(payload)


async def api_arc_deliver(request: web.Request) -> web.Response:
    """POST /api/arcs/{arc_id}/deliver/{entry_id} — re-deliver a stored entry; takes no content."""
    if request.get("internal_auth") is not True:
        return web.json_response(
            {"error": "internal secret required", "code": "internal_auth_required"}, status=403
        )
    arc_id = request.match_info.get("arc_id", "")
    entry_id = request.match_info.get("entry_id", "")

    def work() -> dict[str, Any] | None:
        found = [e for e in arcs.read_entries(arc_id, limit=10**9) if e["id"] == entry_id]
        return found[0] if found else None

    try:
        entry = await asyncio.to_thread(work)
    except (arcs.ArcError, OSError) as exc:
        return _error_response(exc, "arc_deliver")
    if entry is None:
        return web.json_response(
            {"error": f"no entry {entry_id} in arc {arc_id}", "code": "arc_entry_not_found"},
            status=404,
        )
    return web.json_response(
        {"delivery": await deliver_entry(request.app.get("state"), arc_id, entry)}
    )
