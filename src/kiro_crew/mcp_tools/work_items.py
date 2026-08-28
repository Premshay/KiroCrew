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


def schemas() -> list[dict[str, Any]]:
    """Return the advertised half of the coordinator-only tool contract."""
    text = {"type": "string", "maxLength": 2000}
    item_id = {"type": "string", "maxLength": 64}
    return [
        {
            "name": "work_cycle_open",
            "description": "Open THIS coordinator session's one active work cycle.",
            "inputSchema": {
                "type": "object",
                "properties": {"goal": text, "next_action": text},
                "required": ["goal", "next_action"],
            },
        },
        {
            "name": "work_item_create",
            "description": (
                "Create a proposed work item in THIS coordinator's active cycle. "
                "Acceptance is immutable and must be a typed product condition: "
                "pr_checks, file, or human_approval; never a command."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 240},
                    "acceptance": {"type": "object"},
                    "canonical_ref": text,
                    "declared_resources": {"type": "array", "items": {"type": "string"}},
                    "next_action": text,
                },
                "required": ["title", "acceptance", "next_action"],
            },
        },
        {
            "name": "work_item_list",
            "description": "Read THIS coordinator's active cycle, bounded work items, and migration status.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "work_item_read",
            "description": "Read one work item in THIS coordinator's active cycle by opaque ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"item_id": item_id},
                "required": ["item_id"],
            },
        },
        {
            "name": "work_item_update",
            "description": (
                "Update only mutable coordinator fields (reference, resources, next action, "
                "or a progress/blocker event). It cannot change an item's state or acceptance."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": item_id,
                    "canonical_ref": text,
                    "declared_resources": {"type": "array", "items": {"type": "string"}},
                    "next_action": text,
                    "event": text,
                    "event_kind": {"type": "string", "enum": ["progress", "blocker"]},
                },
                "required": ["item_id"],
            },
        },
        {
            "name": "work_item_transition",
            "description": (
                "Coordinator-only transition of one open item to proposed, waiting, rejected, "
                "or cancelled. Accepted is evaluator-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": item_id,
                    "state": {
                        "type": "string",
                        "enum": ["proposed", "waiting", "rejected", "cancelled"],
                    },
                    "event": text,
                    "next_action": text,
                },
                "required": ["item_id", "state", "event"],
            },
        },
        {
            "name": "work_item_evaluate",
            "description": (
                "Evaluate typed acceptance conditions for selected non-terminal items. "
                "Only a recorded product evaluator pass can mark an item accepted."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"item_ids": {"type": "array", "items": item_id, "maxItems": 64}},
                "required": ["item_ids"],
            },
        },
        {
            "name": "work_cycle_close",
            "description": (
                "Close THIS coordinator's active cycle after every item is terminal. "
                "Writes an immutable compact archive and clears only the active cycle."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"summary": text},
                "required": ["summary"],
            },
        },
        {
            "name": "work_cycle_archive_list",
            "description": "List immutable closed-cycle summaries for THIS coordinator.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "work_cycle_archive_read",
            "description": "Read one immutable closed-cycle archive by opaque cycle ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"cycle_id": item_id},
                "required": ["cycle_id"],
            },
        },
        {
            "name": "work_item_launch_candidates",
            "description": (
                "List server-issued launch candidates (live native spawn agents) that "
                "work_item_launch may select. The candidate ID is the only accepted "
                "target handle; raw agent, model, or command inputs are refused."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "work_item_launch",
            "description": (
                "Arm one launched-subagent assignment on an open item and ask the "
                "governed manager to create the child run. Manager acceptance returns "
                "launch_queued; only the child-start receipt creates dispatched. "
                "A refusal leaves the item open with a named failed dispatch."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"item_id": item_id, "candidate_id": {"type": "string", "maxLength": 64}},
                "required": ["item_id", "candidate_id"],
            },
        },
        {
            "name": "work_item_dispatch_retry",
            "description": (
                "Retry the SAME pending or failed launched assignment after target "
                "revalidation. A live queued or running run is never duplicated; a "
                "finished run is refused so it must be revoked and re-launched."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"item_id": item_id},
                "required": ["item_id"],
            },
        },
        {
            "name": "work_item_revoke_assignment",
            "description": (
                "Explicitly revoke the item's current assignment, returning it to "
                "proposed. A queued child run is cancelled; a running one is not."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"item_id": item_id},
                "required": ["item_id"],
            },
        },
        {
            "name": "work_item_assigned_list",
            "description": (
                "Worker-only: list open work items currently assigned to THIS "
                "subagent run. A dashboard session or another run sees nothing."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "work_item_assigned_read",
            "description": (
                "Worker-only: read one work item if it is currently assigned to "
                "THIS subagent run, including its contract context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"item_id": item_id},
                "required": ["item_id"],
            },
        },
        {
            "name": "work_item_report_progress",
            "description": (
                "Worker-only: append one bounded progress or blocker note to the "
                "assigned item. It cannot change state, acceptance, or assignment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": item_id,
                    "text": {"type": "string", "maxLength": 2000},
                    "kind": {"type": "string", "enum": ["progress", "blocker"]},
                },
                "required": ["item_id", "text", "kind"],
            },
        },
        {
            "name": "work_item_submit_handoff",
            "description": (
                "Worker-only: store one bounded proposed handoff (outcome, next "
                "action, 1-8 verification entries; blocker and release_condition "
                "together or neither). It does not complete or accept the item; the "
                "coordinator reviews it and the typed evaluator decides acceptance."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "item_id": item_id,
                    "outcome": {"type": "string", "maxLength": 2000},
                    "next_action": {"type": "string", "maxLength": 2000},
                    "verification": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 512},
                        "minItems": 1,
                        "maxItems": 8,
                    },
                    "canonical_ref": {"type": "string", "maxLength": 2000},
                    "blocker": {"type": "string", "maxLength": 2000},
                    "release_condition": {"type": "string", "maxLength": 2000},
                },
                "required": ["item_id", "outcome", "next_action", "verification"],
            },
        },
    ]


def _strict_session_key() -> tuple[str, str]:
    sk = mcp_core._resolve_session_key_strict()
    if not sk:
        return "", (
            "Error: this session's identity could not be verified strictly, so "
            "coordinator work items are not reachable from here. Subagents "
            "cannot inherit their parent's coordinator authority."
            + mcp_core.strict_identity_diagnosis()
        )
    return sk, ""


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
