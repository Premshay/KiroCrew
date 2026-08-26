"""Strict internal routes for coordinator-owned durable work items.

The session-ledger route already owns the correct durable-session boundary:
recognize the gateway-authored caller identity, refuse restricted modes, then
fold only dashboard prefixes.  These routes reuse that boundary and add no
target-session field, so a coordinator can reach only its own work-item store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from aiohttp import web

from kiro_crew import work_acceptance, work_items
from kiro_crew.dashboard.handlers.session_ledger import _resolve_ledger_key
from kiro_crew.validation import (
    WORK_CYCLE_CLOSE_SCHEMA,
    WORK_CYCLE_OPEN_SCHEMA,
    WORK_ITEM_CREATE_SCHEMA,
    WORK_ITEM_EVALUATE_SCHEMA,
    WORK_ITEM_TRANSITION_ROUTE_SCHEMA,
    WORK_ITEM_UPDATE_ROUTE_SCHEMA,
    ValidationError,
    validate_tool_args,
)

logger = logging.getLogger(__name__)


def _error_response(exc: Exception, operation: str) -> web.Response:
    """Map expected store failures without concealing corruption as absence."""
    if isinstance(exc, work_items.WorkItemNotFound):
        return web.json_response({"error": str(exc), "code": "work_item_not_found"}, status=404)
    if isinstance(exc, work_items.WorkItemStoreCorrupt):
        logger.warning("work-item store corruption during %s", operation, exc_info=True)
        return web.json_response({"error": str(exc), "code": "work_item_store_corrupt"}, status=409)
    if isinstance(exc, work_items.WorkItemArchiveFull):
        return web.json_response({"error": str(exc), "code": "work_item_archive_full"}, status=409)
    if isinstance(exc, work_items.WorkItemError):
        return web.json_response({"error": str(exc), "code": "work_item_validation"}, status=400)
    if isinstance(exc, OSError):
        logger.warning("work-item store unavailable during %s", operation, exc_info=True)
        return web.json_response(
            {"error": "work-item store is busy; try again", "code": "work_item_store_busy"},
            status=503,
        )
    raise exc


async def _body(
    request: web.Request, schema: Any
) -> tuple[dict[str, Any] | None, web.Response | None]:
    try:
        raw = await request.json()
    except Exception:
        return None, web.json_response(
            {"error": "invalid JSON", "code": "invalid_json"}, status=400
        )
    try:
        return validate_tool_args(raw, schema), None
    except ValidationError as exc:
        return None, web.json_response({"error": str(exc), "code": "validation_error"}, status=400)


async def _coordinator(
    request: web.Request, operation: str
) -> tuple[str | None, web.Response | None]:
    return await _resolve_ledger_key(request, operation)


async def _call(
    request: web.Request,
    operation: str,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> web.Response:
    key, refusal = await _coordinator(request, operation)
    if refusal is not None:
        return refusal
    assert key is not None
    try:
        result = await asyncio.to_thread(fn, key, *args, **kwargs)
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, operation)
    return web.json_response({"ok": True, "result": result})


async def api_work_items_list(request: web.Request) -> web.Response:
    """GET /api/work-items — active cycle, bounded item summaries, migration state."""
    return await _call(request, "work_item_list", work_items.read_state)


async def api_work_cycle_open(request: web.Request) -> web.Response:
    body, error = await _body(request, WORK_CYCLE_OPEN_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    return await _call(request, "work_cycle_open", work_items.open_cycle, **body)


async def api_work_item_create(request: web.Request) -> web.Response:
    body, error = await _body(request, WORK_ITEM_CREATE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    return await _call(request, "work_item_create", work_items.create_item, **body)


async def api_work_item_read(request: web.Request) -> web.Response:
    item_id = request.match_info.get("item_id", "")
    return await _call(request, "work_item_read", work_items.read_item, item_id)


async def api_work_item_update(request: web.Request) -> web.Response:
    body, error = await _body(request, WORK_ITEM_UPDATE_ROUTE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    item_id = request.match_info.get("item_id", "")
    return await _call(request, "work_item_update", work_items.update_item, item_id, **body)


async def api_work_item_transition(request: web.Request) -> web.Response:
    body, error = await _body(request, WORK_ITEM_TRANSITION_ROUTE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    item_id = request.match_info.get("item_id", "")
    return await _call(
        request,
        "work_item_transition",
        work_items.transition_item,
        item_id,
        state_name=body["state"],
        event=body["event"],
        next_action=body.get("next_action"),
    )


async def api_work_item_evaluate(request: web.Request) -> web.Response:
    body, error = await _body(request, WORK_ITEM_EVALUATE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    return await _call(
        request,
        "work_item_evaluate",
        work_items.evaluate_items,
        body["item_ids"],
        evaluator=work_acceptance.evaluate,
    )


async def api_work_cycle_close(request: web.Request) -> web.Response:
    body, error = await _body(request, WORK_CYCLE_CLOSE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    return await _call(request, "work_cycle_close", work_items.close_cycle, **body)


async def api_work_cycle_archive_list(request: web.Request) -> web.Response:
    return await _call(request, "work_cycle_archive_list", work_items.list_archives)


async def api_work_cycle_archive_read(request: web.Request) -> web.Response:
    cycle_id = request.match_info.get("cycle_id", "")
    return await _call(request, "work_cycle_archive_read", work_items.read_archive, cycle_id)
