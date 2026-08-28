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

from kiro_crew import work_acceptance, work_dispatch, work_items
from kiro_crew.dashboard.handlers.session_ledger import _resolve_ledger_key
from kiro_crew.sel import sel
from kiro_crew.validation import (
    WORK_CYCLE_CLOSE_SCHEMA,
    WORK_CYCLE_OPEN_SCHEMA,
    WORK_ITEM_ASSIGNED_HANDOFF_ROUTE_SCHEMA,
    WORK_ITEM_ASSIGNED_PROGRESS_ROUTE_SCHEMA,
    WORK_ITEM_CREATE_SCHEMA,
    WORK_ITEM_DISPATCH_RETRY_ROUTE_SCHEMA,
    WORK_ITEM_EVALUATE_SCHEMA,
    WORK_ITEM_LAUNCH_ROUTE_SCHEMA,
    WORK_ITEM_REVOKE_ROUTE_SCHEMA,
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
    if isinstance(exc, work_dispatch.WorkDispatchError):
        return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)
    if isinstance(exc, work_items.WorkItemAssignmentDenied):
        logger.warning("work-item assignment access denied during %s", operation)
        return web.json_response(
            {"error": str(exc), "code": "assignment_access_denied"}, status=403
        )
    if isinstance(exc, work_items.WorkItemAssignmentStale):
        return web.json_response({"error": str(exc), "code": "assignment_stale"}, status=409)
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


def _manager(request: web.Request) -> tuple[Any | None, web.Response | None]:
    """The governed subagent manager, or a named unavailability refusal."""
    manager = request.app["state"].subagents
    if manager is None:
        return None, web.json_response(
            {
                "error": "the subagent manager is unavailable for work-item launch",
                "code": "subagent_manager_unavailable",
            },
            status=503,
        )
    return manager, None


async def _worker(
    request: web.Request, operation: str
) -> tuple[str | None, web.Response | None]:
    """Strict worker gate: only a manager-attributed ``subagent:<run-id>`` key.

    The coordinator recognition cascade cannot see subagent runs, so this
    family uses a shape-strict gate instead; ownership of the specific item
    is then revalidated under the store lock, so a well-formed key that was
    never assigned gets nothing.
    """
    sk = request.headers.get("X-Session-Key", "")
    if not isinstance(sk, str) or not work_items._WORKER_KEY_RE.fullmatch(sk):
        try:
            sel().log_api_access(
                caller=sk or "anonymous",
                operation=operation,
                outcome="denied",
                source="dashboard",
                resources="worker_identity_invalid",
            )
        except Exception:
            logger.warning("worker-gate audit failed for %s", operation, exc_info=True)
        return None, web.json_response(
            {"error": "worker identity is invalid for this route", "code": "worker_identity_invalid"},
            status=403,
        )
    return sk, None


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


async def api_work_item_launch_candidates(request: web.Request) -> web.Response:
    """GET /api/work-items/launch/candidates — live spawn-roster handles."""
    key, refusal = await _coordinator(request, "work_item_launch_candidates")
    if refusal is not None:
        return refusal
    try:
        candidates = await asyncio.to_thread(work_dispatch.launch_candidates)
    except Exception:
        logger.warning("launch-candidate roster failed", exc_info=True)
        return web.json_response(
            {"error": "the launch-candidate roster is unavailable", "code": "subagent_manager_unavailable"},
            status=503,
        )
    return web.json_response({"ok": True, "result": {"candidates": candidates}})


async def api_work_item_launch(request: web.Request) -> web.Response:
    """POST /api/work-items/items/{item_id}/launch — one governed child launch."""
    body, error = await _body(request, WORK_ITEM_LAUNCH_ROUTE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    item_id = request.match_info.get("item_id", "")
    key, refusal = await _coordinator(request, "work_item_launch")
    if refusal is not None:
        return refusal
    manager, refusal = _manager(request)
    if refusal is not None:
        return refusal
    try:
        result = await work_dispatch.launch(key, item_id, body["candidate_id"], manager)
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, "work_item_launch")
    return web.json_response({"ok": True, "result": result})


async def api_work_item_dispatch_retry(request: web.Request) -> web.Response:
    """POST /api/work-items/items/{item_id}/dispatch-retry — same assignment only."""
    item_id = request.match_info.get("item_id", "")
    key, refusal = await _coordinator(request, "work_item_dispatch_retry")
    if refusal is not None:
        return refusal
    manager, refusal = _manager(request)
    if refusal is not None:
        return refusal
    try:
        result = await work_dispatch.dispatch_retry(key, item_id, manager)
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, "work_item_dispatch_retry")
    return web.json_response({"ok": True, "result": result})


async def api_work_item_revoke_assignment(request: web.Request) -> web.Response:
    """POST /api/work-items/items/{item_id}/revoke-assignment — explicit revocation."""
    item_id = request.match_info.get("item_id", "")
    key, refusal = await _coordinator(request, "work_item_revoke_assignment")
    if refusal is not None:
        return refusal
    manager, refusal = _manager(request)
    if refusal is not None:
        return refusal
    try:
        result = await work_dispatch.revoke_assignment(key, item_id, manager)
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, "work_item_revoke_assignment")
    return web.json_response({"ok": True, "result": result})


async def api_work_item_assigned_list(request: web.Request) -> web.Response:
    """GET /api/work-items/assigned — open items assigned to this worker run."""
    key, refusal = await _worker(request, "work_item_assigned_list")
    if refusal is not None:
        return refusal
    assert key is not None
    try:
        result = await asyncio.to_thread(work_items.worker_assigned_list, key)
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, "work_item_assigned_list")
    return web.json_response({"ok": True, "result": result})


async def api_work_item_assigned_read(request: web.Request) -> web.Response:
    """GET /api/work-items/assigned/{item_id} — one assignment owned by this run."""
    key, refusal = await _worker(request, "work_item_assigned_read")
    if refusal is not None:
        return refusal
    assert key is not None
    item_id = request.match_info.get("item_id", "")
    try:
        result = await asyncio.to_thread(work_items.worker_assigned_read, key, item_id)
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, "work_item_assigned_read")
    return web.json_response({"ok": True, "result": result})


async def api_work_item_report_progress(request: web.Request) -> web.Response:
    """POST /api/work-items/assigned/{item_id}/progress — one bounded note."""
    body, error = await _body(request, WORK_ITEM_ASSIGNED_PROGRESS_ROUTE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    key, refusal = await _worker(request, "work_item_report_progress")
    if refusal is not None:
        return refusal
    assert key is not None
    item_id = request.match_info.get("item_id", "")
    try:
        result = await asyncio.to_thread(
            work_items.worker_report_progress, key, item_id, body["text"], body["kind"]
        )
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, "work_item_report_progress")
    return web.json_response({"ok": True, "result": result})


async def api_work_item_submit_handoff(request: web.Request) -> web.Response:
    """POST /api/work-items/assigned/{item_id}/handoff — one bounded proposal."""
    body, error = await _body(request, WORK_ITEM_ASSIGNED_HANDOFF_ROUTE_SCHEMA)
    if error is not None:
        return error
    assert body is not None
    key, refusal = await _worker(request, "work_item_submit_handoff")
    if refusal is not None:
        return refusal
    assert key is not None
    item_id = request.match_info.get("item_id", "")
    try:
        result = await asyncio.to_thread(
            work_items.worker_submit_handoff,
            key,
            item_id,
            outcome=body.get("outcome", ""),
            next_action=body.get("next_action", ""),
            verification=body.get("verification", []),
            canonical_ref=body.get("canonical_ref", ""),
            blocker=body.get("blocker", ""),
            release_condition=body.get("release_condition", ""),
        )
    except (work_items.WorkItemError, OSError) as exc:
        return _error_response(exc, "work_item_submit_handoff")
    return web.json_response({"ok": True, "result": result})
