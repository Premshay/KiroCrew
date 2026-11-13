"""Coordinator work-item tools backed by product-owned durable storage.

These tools intentionally have no ``session_key``, assignment, command, or
shell fields.  The strict caller identity is both the authorization boundary
and the only coordinate used by the gateway routes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.catalog.work_items import schemas  # noqa: F401 - shared declaration owner


def _strict_session_key() -> tuple[str, str]:
    return mcp_core.require_strict_session_key(
        "Error: this session's identity could not be verified strictly, so "
        "coordinator work items are not reachable from here. Subagents "
        "cannot inherit their parent's coordinator authority."
    )


def _response(payload: dict[str, Any]) -> str:
    err = payload.get("error")
    if err:
        return f"Error: {err}"
    return json.dumps(payload.get("result"), indent=2)


def _get(path: str) -> str:
    sk, err = _strict_session_key()
    return err if err else _response(mcp_core._get(path, session_key=sk))


def _post(path: str, payload: dict[str, Any]) -> str:
    sk, err = _strict_session_key()
    return err if err else _response(mcp_core._post(path, payload, session_key=sk))


def work_cycle_open(name: str, args: dict[str, Any]) -> str:
    return _post("/api/work-items/cycle/open", args)


def work_item_create(name: str, args: dict[str, Any]) -> str:
    return _post("/api/work-items/items", args)


def work_item_list(name: str, args: dict[str, Any]) -> str:
    return _get("/api/work-items")


def work_item_read(name: str, args: dict[str, Any]) -> str:
    return _get(f"/api/work-items/items/{args['item_id']}")


def work_item_update(name: str, args: dict[str, Any]) -> str:
    item_id = args["item_id"]
    payload = {key: value for key, value in args.items() if key != "item_id"}
    return _post(f"/api/work-items/items/{item_id}/update", payload)


def work_item_transition(name: str, args: dict[str, Any]) -> str:
    item_id = args["item_id"]
    payload = {key: value for key, value in args.items() if key != "item_id"}
    return _post(f"/api/work-items/items/{item_id}/transition", payload)


def work_item_evaluate(name: str, args: dict[str, Any]) -> str:
    return _post("/api/work-items/items/evaluate", args)


def work_cycle_close(name: str, args: dict[str, Any]) -> str:
    return _post("/api/work-items/cycle/close", args)


def work_cycle_archive_list(name: str, args: dict[str, Any]) -> str:
    return _get("/api/work-items/archive")


def work_cycle_archive_read(name: str, args: dict[str, Any]) -> str:
    return _get(f"/api/work-items/archive/{args['cycle_id']}")


def work_item_launch_candidates(name: str, args: dict[str, Any]) -> str:
    return _get("/api/work-items/launch/candidates")


def work_item_launch(name: str, args: dict[str, Any]) -> str:
    item_id = args["item_id"]
    return _post(f"/api/work-items/items/{item_id}/launch", {"candidate_id": args["candidate_id"]})


def work_item_dispatch_retry(name: str, args: dict[str, Any]) -> str:
    item_id = args["item_id"]
    return _post(f"/api/work-items/items/{item_id}/dispatch-retry", {})


def work_item_revoke_assignment(name: str, args: dict[str, Any]) -> str:
    item_id = args["item_id"]
    return _post(f"/api/work-items/items/{item_id}/revoke-assignment", {})


def work_item_assigned_list(name: str, args: dict[str, Any]) -> str:
    return _get("/api/work-items/assigned")


def work_item_assigned_read(name: str, args: dict[str, Any]) -> str:
    return _get(f"/api/work-items/assigned/{args['item_id']}")


def work_item_report_progress(name: str, args: dict[str, Any]) -> str:
    item_id = args["item_id"]
    payload = {key: value for key, value in args.items() if key != "item_id"}
    return _post(f"/api/work-items/assigned/{item_id}/progress", payload)


def work_item_submit_handoff(name: str, args: dict[str, Any]) -> str:
    item_id = args["item_id"]
    payload = {key: value for key, value in args.items() if key != "item_id"}
    return _post(f"/api/work-items/assigned/{item_id}/handoff", payload)


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "work_cycle_open": work_cycle_open,
    "work_item_create": work_item_create,
    "work_item_list": work_item_list,
    "work_item_read": work_item_read,
    "work_item_update": work_item_update,
    "work_item_transition": work_item_transition,
    "work_item_evaluate": work_item_evaluate,
    "work_cycle_close": work_cycle_close,
    "work_cycle_archive_list": work_cycle_archive_list,
    "work_cycle_archive_read": work_cycle_archive_read,
    "work_item_launch_candidates": work_item_launch_candidates,
    "work_item_launch": work_item_launch,
    "work_item_dispatch_retry": work_item_dispatch_retry,
    "work_item_revoke_assignment": work_item_revoke_assignment,
    "work_item_assigned_list": work_item_assigned_list,
    "work_item_assigned_read": work_item_assigned_read,
    "work_item_report_progress": work_item_report_progress,
    "work_item_submit_handoff": work_item_submit_handoff,
}
