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
from dataclasses import dataclass
from typing import Any

from kiro_crew import work_items
from kiro_crew.agent_discovery import list_agents
from kiro_crew.sel import sel
from kiro_crew.session_ledger import ledger_key

logger = logging.getLogger(__name__)


class WorkDispatchError(work_items.WorkItemError):
    """A dispatch outcome with a machine-readable code and HTTP status."""

    def __init__(self, message: str, *, code: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LaunchCandidate:
    """One product-owned worker class with its fixed runtime envelope."""

    id: str
    worker_class: str
    agent_name: str
    contract_max_bytes: int
    max_turns: int


_FAST_RECON = LaunchCandidate(
    id=_fingerprint("work-item-worker:fast-recon:v1"),
    worker_class="fast_recon",
    agent_name="kirocrew-fast",
    contract_max_bytes=8 * 1024,
    max_turns=12,
)
_CANDIDATES = (_FAST_RECON,)


def _live_candidates() -> tuple[LaunchCandidate, ...]:
    """Return classes whose generated, model-pinned worker spec is live.

    The roster is re-read for every candidate query and launch. A caller never
    selects an arbitrary installed agent: the fixed class registry below owns
    the internal agent name, context scope, turn cap, and contract ceiling.
    """
    installed = {agent.name: agent for agent in list_agents()}
    return tuple(
        candidate
        for candidate in _CANDIDATES
        if (
            (agent := installed.get(candidate.agent_name)) is not None
            and agent.model == "fast"
            and "kirocrew-core" in agent.mcp_servers
        )
    )


def launch_candidates() -> list[dict[str, Any]]:
    """Live product worker classes as opaque, server-issued candidates.

    The handle resolves against a fresh registry at launch time. The response
    deliberately contains neither an agent name nor a model name.
    """
    return [
        {
            "id": candidate.id,
            "worker_class": candidate.worker_class,
            "contract_max_bytes": candidate.contract_max_bytes,
        }
        for candidate in _live_candidates()
    ]


def _resolve_candidate(candidate_id: str) -> LaunchCandidate | None:
    if not isinstance(candidate_id, str) or not candidate_id:
        return None
    for candidate in _live_candidates():
        if candidate.id == candidate_id:
            return candidate
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
    candidate = _resolve_candidate(candidate_id)
    if candidate is None:
        _audit_caller(session_key, "work_item.launch", "worker_target_unavailable")
        raise WorkDispatchError(
            "the launch candidate is not a live agent",
            code="worker_target_unavailable",
            status=400,
        )
    _register_hook(manager)
    try:
        item, contract = await asyncio.to_thread(
            work_items.launch_item,
            session_key,
            item_id,
            candidate_id,
            contract_max_bytes=candidate.contract_max_bytes,
            exclusive_candidate_fingerprint=candidate.id,
        )
    except work_items.WorkItemContractTooLarge as exc:
        _audit_caller(session_key, "work_item.launch", "worker_contract_too_large")
        raise WorkDispatchError(
            "the selected worker class cannot receive this work item contract",
            code="worker_contract_too_large",
            status=400,
        ) from exc
    except work_items.WorkItemWorkerClassBusy as exc:
        _audit_caller(session_key, "work_item.launch", "fast_worker_busy")
        raise WorkDispatchError(
            "the fast reconnaissance worker already has a live assignment",
            code="fast_worker_busy",
            status=409,
        ) from exc
    run_id = item["assignment"]["worker_run_id"]
    try:
        info = manager.spawn(
            contract,
            parent_session_key=session_key,
            agent=candidate.agent_name,
            max_turns=candidate.max_turns,
            include_memory=False,
            include_lessons=False,
            include_project=False,
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
    # Unknown: the manager has no record of this run. Re-arm the same run ID
    # only after resolving its original server-owned class again.
    candidate = _resolve_candidate(assignment["candidate_fingerprint"])
    if candidate is None:
        _audit_caller(session_key, "work_item.dispatch_retry", "worker_target_changed", item)
        raise WorkDispatchError(
            "the launch candidate is no longer a live agent",
            code="worker_target_changed",
            status=409,
        )
    try:
        item, contract = await asyncio.to_thread(
            work_items.arm_launch_retry,
            session_key,
            item_id,
            contract_max_bytes=candidate.contract_max_bytes,
        )
    except work_items.WorkItemContractTooLarge as exc:
        _audit_caller(session_key, "work_item.dispatch_retry", "worker_contract_too_large", item)
        raise WorkDispatchError(
            "the selected worker class cannot receive this work item contract",
            code="worker_contract_too_large",
            status=400,
        ) from exc
    try:
        info = manager.spawn(
            contract,
            parent_session_key=session_key,
            agent=candidate.agent_name,
            max_turns=candidate.max_turns,
            include_memory=False,
            include_lessons=False,
            include_project=False,
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
    """Revoke one assignment without allowing a still-running replacement race."""
    item = await asyncio.to_thread(work_items.read_item, session_key, item_id)
    assignment = item.get("assignment")
    worker_may_still_run = False
    if assignment is not None and assignment["status"] in work_items.ASSIGNMENT_ACTIVE:
        run_id = assignment["worker_run_id"]
        run_state = getattr(manager, "run_state", None)
        if not callable(run_state):
            if assignment["status"] == work_items.ASSIGNMENT_DELIVERED:
                worker_may_still_run = True
                state = "running"
            else:
                _audit_caller(session_key, "work_item.revoke_assignment", "queued_cancel_unavailable", item)
                raise WorkDispatchError(
                    "the queued worker cannot be cancelled until manager state is available",
                    code="queued_cancel_unavailable",
                    status=503,
                )
        else:
            try:
                state = await asyncio.to_thread(run_state, run_id)
            except Exception as exc:
                # A delivered child may still be running even though the
                # manager's inspection surface is temporarily unavailable.
                # Revoke its authority but retain the exclusive class slot.
                # A queued launch, in contrast, must stay retryable: marking
                # it revoked without confirming cancellation could let it
                # start later under a supposedly-revoked assignment.
                if assignment["status"] == work_items.ASSIGNMENT_DELIVERED:
                    worker_may_still_run = True
                    state = "running"
                else:
                    _audit_caller(
                        session_key,
                        "work_item.revoke_assignment",
                        "queued_cancel_unavailable",
                        item,
                    )
                    raise WorkDispatchError(
                        "the queued worker cannot be cancelled until manager state is available",
                        code="queued_cancel_unavailable",
                        status=503,
                    ) from exc
        if state == "queued":
            cancel = getattr(manager, "cancel", None)
            if not callable(cancel):
                _audit_caller(session_key, "work_item.revoke_assignment", "queued_cancel_unavailable", item)
                raise WorkDispatchError(
                    "the queued worker cannot be cancelled until manager cancellation is available",
                    code="queued_cancel_unavailable",
                    status=503,
                )
            try:
                cancelled = await cancel(run_id)
            except Exception as exc:
                logger.warning("cancel of queued run %s failed", run_id, exc_info=True)
                _audit_caller(session_key, "work_item.revoke_assignment", "queued_cancel_unavailable", item)
                raise WorkDispatchError(
                    "the queued worker cancellation was unavailable; retry revocation",
                    code="queued_cancel_unavailable",
                    status=503,
                ) from exc
            if cancelled is not True:
                _audit_caller(session_key, "work_item.revoke_assignment", "queued_cancel_failed", item)
                raise WorkDispatchError(
                    "the queued worker was not cancelled; retry revocation",
                    code="queued_cancel_failed",
                    status=409,
                )
        elif state == "running":
            worker_may_still_run = True
        elif state not in {"terminal", "unknown"}:
            # Treat a malformed state exactly like an unavailable inspection:
            # fail closed for queued work and preserve the class slot for a
            # delivered child.  This keeps an unexpected manager regression
            # from creating the same replacement race as a failed cancel.
            if assignment["status"] == work_items.ASSIGNMENT_DELIVERED:
                worker_may_still_run = True
            else:
                _audit_caller(
                    session_key,
                    "work_item.revoke_assignment",
                    "queued_cancel_unavailable",
                    item,
                )
                raise WorkDispatchError(
                    "the queued worker cannot be cancelled until manager state is available",
                    code="queued_cancel_unavailable",
                    status=503,
                )
    item = await asyncio.to_thread(
        work_items.revoke_assignment,
        session_key,
        item_id,
        worker_may_still_run=worker_may_still_run,
    )
    outcome = "revoked_running" if worker_may_still_run else "revoked"
    _audit_caller(session_key, "work_item.revoke_assignment", outcome, item)
    return {"code": outcome, "item": item}


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
