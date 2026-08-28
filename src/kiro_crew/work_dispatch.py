"""Launched-subagent dispatch adapter for coordinator work items.

This is the only place where a coordinator's work item meets the subagent
manager. It resolves a server-issued launch candidate to a live agent name,
arms the store's durable assignment, hands the rendered contract to
``SubagentManager.spawn`` with product defaults for everything else, and
translates the manager's receipt events into store state. The manager applies
its existing governance, allowlist, capacity, and approval rules; this module
never synthesizes a spawn command or forwards caller-controlled spawn
arguments. The worker half of the surface (assigned list/read/progress/
handoff) is served by the store itself under the exact ``subagent:<run-id>``
key the manager attributes to the child.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from kiro_crew import work_items
from kiro_crew.agent_discovery import list_agents
from kiro_crew.sel import sel
from kiro_crew.session_ledger import ledger_key
from kiro_crew.subagent import UNADVERTISED_AGENTS

logger = logging.getLogger(__name__)


class WorkDispatchError(work_items.WorkItemError):
    """A dispatch outcome with a machine-readable code and HTTP status."""

    def __init__(self, message: str, *, code: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _live_agent_names() -> set[str]:
    names = {agent.name for agent in list_agents()}
    return names - UNADVERTISED_AGENTS


def launch_candidates() -> list[dict[str, str]]:
    """Live spawn-roster agents as server-issued candidates.

    The candidate ID is the stable handle ``work_item_launch`` accepts; it
    resolves against a fresh roster at launch time, so a stale candidate
    cannot select a removed or renamed agent.
    """
    return [{"id": _fingerprint(name), "agent": name} for name in sorted(_live_agent_names())]


def _resolve_candidate(candidate_id: str) -> str | None:
    if not isinstance(candidate_id, str) or not candidate_id:
        return None
    for name in _live_agent_names():
        if _fingerprint(name) == candidate_id:
            return name
    return None


def _audit(
    caller: str, operation: str, outcome: str, item_id: str = "", assignment_id: str = ""
) -> None:
    """SEL-audit one dispatch operation without retaining any work content."""
    resources = " ".join(
        part
        for part in (
            f"work_item:{_fingerprint(item_id)}" if item_id else "",
            f"assignment:{_fingerprint(assignment_id)}" if assignment_id else "",
        )
        if part
    )
    try:
        sel().log_api_access(
            caller=_fingerprint(caller),
            operation=operation,
            outcome=outcome,
            source="work_dispatch",
            resources=resources,
        )
    except Exception:
        logger.warning("work-dispatch audit failed for %s", operation, exc_info=True)


def _audit_caller(session_key: str, operation: str, outcome: str, item: dict[str, Any] | None = None) -> None:
    _audit(
        ledger_key(session_key),
        operation,
        outcome,
        item["id"] if item else "",
        item["assignment"]["id"] if item and item.get("assignment") else "",
    )


def _register_hook(manager: Any) -> None:
    add = getattr(manager, "add_event_hook", None)
    if callable(add):
        try:
            add(on_subagent_event)
        except Exception:
            logger.warning("failed to register the work-dispatch event hook", exc_info=True)


def _classify_rejection(message: str) -> str:
    """Bound a manager refusal reason to a named, stored failure code."""
    low = message.lower()
    if "capacity" in low or "concurrent" in low or "queue" in low:
        return "launch_capacity"
    if "agent" in low and ("not found" in low or "unknown" in low or "invalid" in low):
        return "agent_unavailable"
    return "worker_launch_refused"


async def launch(
    session_key: str, item_id: str, candidate_id: str, manager: Any
) -> dict[str, Any]:
    """Arm the assignment, ask the governed manager to launch, record the receipt."""
    agent_name = _resolve_candidate(candidate_id)
    if agent_name is None:
        _audit_caller(session_key, "work_item.launch", "worker_target_unavailable")
        raise WorkDispatchError(
            "the launch candidate is not a live agent",
            code="worker_target_unavailable",
            status=400,
        )
    _register_hook(manager)
    item, contract = await asyncio.to_thread(
        work_items.launch_item, session_key, item_id, candidate_id
    )
    run_id = item["assignment"]["worker_run_id"]
    try:
        info = manager.spawn(
            contract,
            parent_session_key=session_key,
            agent=agent_name,
            _preassigned_id=run_id,
        )
    except Exception as exc:
        failure_code = _classify_rejection(str(exc))
        await asyncio.to_thread(
            work_items.record_launch_failed, session_key, run_id, failure_code
        )
        _audit_caller(session_key, "work_item.launch", "dispatch_delivery_failed", item)
        raise WorkDispatchError(
            "the subagent manager rejected the launch", code=failure_code, status=502
        ) from exc
    if info is None or info.error:
        failure_code = _classify_rejection(str(info.error if info is not None else "no run was returned"))
        await asyncio.to_thread(
            work_items.record_launch_failed, session_key, run_id, failure_code
        )
        item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
        _audit_caller(session_key, "work_item.launch", "dispatch_delivery_failed", item)
        return {"code": "dispatch_delivery_failed", "item": item}
    await asyncio.to_thread(work_items.record_launch_accepted, session_key, run_id)
    item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
    _audit_caller(session_key, "work_item.launch", "launch_queued", item)
    return {"code": "launch_queued", "item": item}


async def dispatch_retry(session_key: str, item_id: str, manager: Any) -> dict[str, Any]:
    """Recover the same assignment without ever starting a second child.

    The manager is asked for the preallocated run's live state first. A queued
    or running run is never duplicated; a dead run is refused so the
    coordinator must revoke and launch a new assignment; an unknown run may
    be resubmitted under the same run ID with the stored candidate.
    """
    item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
    assignment = item.get("assignment")
    if assignment is None:
        raise WorkDispatchError(
            "the work item has no assignment to retry",
            code="worker_target_unavailable",
            status=400,
        )
    if assignment["status"] == work_items.ASSIGNMENT_DELIVERED:
        _audit_caller(session_key, "work_item.dispatch_retry", "dispatched", item)
        return {"code": "dispatched", "item": item}
    if assignment["status"] not in {
        work_items.ASSIGNMENT_PENDING_DELIVERY,
        work_items.ASSIGNMENT_FAILED,
    }:
        _audit_caller(session_key, "work_item.dispatch_retry", "assignment_stale", item)
        raise WorkDispatchError(
            "the assignment is not eligible for a dispatch retry",
            code="assignment_stale",
            status=409,
        )
    run_id = assignment["worker_run_id"]
    _register_hook(manager)
    state = await asyncio.to_thread(manager.run_state, run_id)
    if state == "queued":
        # The manager owns a live run; take the acceptance receipt if the
        # store can still take it, then report the manager's truth.
        await asyncio.to_thread(work_items.record_launch_accepted, session_key, run_id)
        item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
        _audit_caller(session_key, "work_item.dispatch_retry", "launch_queued", item)
        return {"code": "launch_queued", "item": item}
    if state == "running":
        await asyncio.to_thread(work_items.record_launch_delivered, session_key, run_id)
        item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
        _audit_caller(session_key, "work_item.dispatch_retry", "dispatched", item)
        return {"code": "dispatched", "item": item}
    if state == "terminal":
        _audit_caller(session_key, "work_item.dispatch_retry", "worker_launch_refused", item)
        raise WorkDispatchError(
            "the subagent run has ended; revoke the assignment before launching a replacement",
            code="worker_launch_refused",
            status=409,
        )
    # Unknown: the manager has no record of this run. Re-arm the same run ID.
    item = await asyncio.to_thread(work_items.arm_launch_retry, session_key, item_id)
    agent_name = _resolve_candidate(assignment["candidate_fingerprint"])
    if agent_name is None:
        await asyncio.to_thread(
            work_items.record_launch_failed, session_key, run_id, "worker_target_changed"
        )
        _audit_caller(session_key, "work_item.dispatch_retry", "worker_target_changed", item)
        raise WorkDispatchError(
            "the launch candidate is no longer a live agent",
            code="worker_target_changed",
            status=409,
        )
    item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
    contract = work_items.render_contract(item, item["assignment"])
    try:
        info = manager.spawn(
            contract,
            parent_session_key=session_key,
            agent=agent_name,
            _preassigned_id=run_id,
        )
    except Exception as exc:
        failure_code = _classify_rejection(str(exc))
        await asyncio.to_thread(
            work_items.record_launch_failed, session_key, run_id, failure_code
        )
        _audit_caller(session_key, "work_item.dispatch_retry", "dispatch_delivery_failed", item)
        raise WorkDispatchError(
            "the subagent manager rejected the retry", code=failure_code, status=502
        ) from exc
    if info is None or info.error:
        failure_code = _classify_rejection(str(info.error if info is not None else "no run was returned"))
        await asyncio.to_thread(
            work_items.record_launch_failed, session_key, run_id, failure_code
        )
        item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
        _audit_caller(session_key, "work_item.dispatch_retry", "dispatch_delivery_failed", item)
        return {"code": "dispatch_delivery_failed", "item": item}
    await asyncio.to_thread(work_items.record_launch_accepted, session_key, run_id)
    item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
    _audit_caller(session_key, "work_item.dispatch_retry", "launch_queued", item)
    return {"code": "launch_queued", "item": item}


async def revoke_assignment(session_key: str, item_id: str, manager: Any) -> dict[str, Any]:
    """Revoke the live assignment; cancel only a queued run, never a live one."""
    item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
    assignment = item.get("assignment")
    if assignment is not None and assignment["status"] == work_items.ASSIGNMENT_LAUNCH_QUEUED:
        run_id = assignment["worker_run_id"]
        state = await asyncio.to_thread(
            getattr(manager, "run_state", lambda _id: "queued"), run_id
        )
        if state == "queued":
            cancel = getattr(manager, "cancel", None)
            if callable(cancel):
                try:
                    await cancel(run_id)
                except Exception:
                    logger.warning(
                        "cancel of queued run %s failed; revocation recorded anyway", run_id, exc_info=True
                    )
    item = await asyncio.to_thread(work_items.revoke_assignment, session_key, item_id)
    _audit_caller(session_key, "work_item.revoke_assignment", "revoked", item)
    return {"code": "revoked", "item": item}


async def on_subagent_event(etype: str, info: Any, extra: Any) -> None:
    """Manager receipt hook: translate child start/end into store receipts.

    The parent key is the coordinator's folded key; the run ID is the
    preallocated child identity. Both receipts are no-ops for unknown runs or
    assignments that already moved on, which makes duplicate events safe.
    """
    try:
        if etype not in {"subagent_spawn", "subagent_done"}:
            return
        parent_key = str(getattr(info, "parent_session_key", "") or "")
        if not parent_key:
            return
        run_id = str(getattr(info, "id", "") or "")
        if etype == "subagent_spawn":
            await asyncio.to_thread(work_items.record_launch_delivered, parent_key, run_id)
            return
        outcome = ""
        if isinstance(extra, dict):
            outcome = str(extra.get("outcome") or "")
        if not outcome:
            outcome = str(getattr(info, "outcome", "") or "")
        await asyncio.to_thread(
            work_items.record_runtime_event,
            parent_key,
            run_id,
            "child run ended: " + (outcome or "ended"),
        )
    except Exception:
        logger.warning(
            "work-dispatch receipt handling failed for %s", etype, exc_info=True
        )
