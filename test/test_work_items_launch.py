"""Launched-subagent assignment, receipt, and worker-surface contracts (Slice 3)."""

from __future__ import annotations

import json

import pytest

from kiro_crew import work_items as wi

KEY = "dashboard_chat-9-999"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _create(key: str = KEY, **overrides) -> dict:
    wi.open_cycle(key, goal="Coordinate a focused repair", next_action="Create work items")
    return wi.create_item(
        key,
        title=overrides.get("title", "Repair the endpoint"),
        acceptance=overrides.get(
            "acceptance", {"kind": "file", "path": "/tmp/proof", "exists": True}
        ),
        next_action=overrides.get("next_action", "Implement and verify the repair"),
    )


def _launch(key: str = KEY) -> tuple[dict, str]:
    item = _create(key)
    return wi.launch_item(key, item["id"])


def _worker_key(item: dict) -> str:
    return item["assignment"]["worker_session_key"]


def test_slice2_records_round_trip_with_null_assignment():
    item = _create()
    dir_path = wi.coordinator_dir(KEY)
    state = json.loads((dir_path / "state.json").read_text(encoding="utf-8"))
    for raw in state["active_cycle"]["items"]:
        raw.pop("assignment", None)
        raw.pop("worker_handoff", None)
    (dir_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    read = wi.read_item(KEY, item["id"])
    assert read["assignment"] is None
    assert read["worker_handoff"] is None
    launched, contract = wi.launch_item(KEY, item["id"])
    assert launched["assignment"]["status"] == wi.ASSIGNMENT_PENDING_DELIVERY
    assert "Work item: " in contract


def test_launch_arms_pending_delivery_with_stable_contract():
    item, contract = _launch()
    assignment = item["assignment"]
    assert assignment["status"] == wi.ASSIGNMENT_PENDING_DELIVERY
    assert assignment["source"] == wi.ASSIGNMENT_SOURCE_LAUNCHED
    assert assignment["attempt"] == 1
    assert assignment["worker_run_id"] == assignment["worker_session_key"].split(":", 1)[1]
    assert len(assignment["worker_run_id"]) == 8
    assert len(assignment["worker_fingerprint"]) == 16
    assert len(assignment["contract_digest"]) == 64
    assert assignment["contract_digest"] == __import__("hashlib").sha256(
        contract.encode("utf-8")
    ).hexdigest()
    assert item["title"] in contract
    assert item["next_action"] in contract
    # Deterministic re-render under the same assignment: identical digest.
    again = wi.render_contract(item, assignment)
    assert again == contract


def test_launch_refuses_a_second_live_assignment():
    item, _ = _launch()
    with pytest.raises(wi.WorkItemError, match="active assignment"):
        wi.launch_item(KEY, item["id"])


def test_launch_refuses_missing_cycle_and_terminal_items():
    with pytest.raises(wi.WorkItemNotFound):
        wi.launch_item("dashboard_chat-0", "wi_" + "0" * 32)
    item = _create()
    wi.transition_item(KEY, item["id"], state_name="cancelled", event="abandoned")
    with pytest.raises(wi.WorkItemError, match="terminal"):
        wi.launch_item(KEY, item["id"])


def test_receipt_chain_to_dispatched_is_idempotent():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]

    wi.record_launch_accepted(KEY, run_id)
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_LAUNCH_QUEUED
    assert read["state"] == wi.STATE_PROPOSED

    wi.record_launch_delivered(KEY, run_id)
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_DELIVERED
    assert read["assignment"]["delivered_at"]
    assert read["state"] == wi.STATE_DISPATCHED
    events = read["events"]

    # A duplicate child-start receipt is a silent no-op.
    wi.record_launch_delivered(KEY, run_id)
    wi.record_launch_accepted(KEY, run_id)
    read = wi.read_item(KEY, item["id"])
    assert read["events"] == events
    assert read["assignment"]["status"] == wi.ASSIGNMENT_DELIVERED


def test_launch_failed_from_pending_and_from_queued():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_failed(KEY, run_id, "launch_capacity")
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_FAILED
    assert read["assignment"]["failure_code"] == "launch_capacity"
    assert read["state"] == wi.STATE_PROPOSED

    # Second item of the same cycle: failed from the queued state.
    item2 = wi.create_item(
        KEY,
        title="Repair the second endpoint",
        acceptance={"kind": "file", "path": "/tmp/proof2", "exists": True},
        next_action="Implement and verify the second repair",
    )
    launched2, _ = wi.launch_item(KEY, item2["id"])
    run2 = launched2["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run2)
    wi.record_launch_failed(KEY, run2, "worker_launch_refused")
    read2 = wi.read_item(KEY, item2["id"])
    assert read2["assignment"]["status"] == wi.ASSIGNMENT_FAILED
    assert read2["assignment"]["failure_code"] == "worker_launch_refused"
    assert read2["assignment"]["delivered_at"] == ""


def test_runtime_event_never_changes_item_state():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    # Before delivery: silent no-op (the arm event already exists).
    baseline = wi.read_item(KEY, item["id"])["events"]
    wi.record_runtime_event(KEY, run_id, "child run ended: crashed")
    assert wi.read_item(KEY, item["id"])["events"] == baseline

    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    wi.record_runtime_event(KEY, run_id, "child run ended: completed")
    read = wi.read_item(KEY, item["id"])
    assert read["state"] == wi.STATE_DISPATCHED
    assert read["events"][-1]["kind"] == "runtime"
    assert "child run ended: completed" in read["events"][-1]["text"]


def test_dispatched_blocks_close_and_coordinator_transitions():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)

    with pytest.raises(wi.WorkItemError, match="terminal"):
        wi.close_cycle(KEY, summary="too early")
    with pytest.raises(wi.WorkItemError, match="may not transition"):
        wi.transition_item(KEY, item["id"], state_name="dispatched", event="impossible")

    moved = wi.transition_item(KEY, item["id"], state_name="waiting", event="reviewing")
    assert moved["state"] == wi.STATE_WAITING


def test_revoke_returns_item_to_proposed_and_allows_relaunch():
    item, _ = _launch()
    first_run = item["assignment"]["worker_run_id"]
    first_id = item["assignment"]["id"]

    revoked = wi.revoke_assignment(KEY, item["id"])
    assert revoked["assignment"]["status"] == wi.ASSIGNMENT_REVOKED
    assert revoked["state"] == wi.STATE_PROPOSED
    with pytest.raises(wi.WorkItemError, match="already revoked"):
        wi.revoke_assignment(KEY, item["id"])

    relaunched = wi.launch_item(KEY, item["id"])
    assert relaunched[0]["assignment"]["worker_run_id"] != first_run
    assert relaunched[0]["assignment"]["id"] != first_id
    assert relaunched[0]["assignment"]["attempt"] == 1


def test_arm_launch_retry_increments_attempt_and_clears_failure():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_failed(KEY, run_id, "launch_capacity")

    armed = wi.arm_launch_retry(KEY, item["id"])
    assert armed["assignment"]["attempt"] == 2
    assert armed["assignment"]["status"] == wi.ASSIGNMENT_PENDING_DELIVERY
    assert armed["assignment"]["failure_code"] == ""

    wi.record_launch_failed(KEY, run_id, "launch_capacity")
    # Delivered and revoked assignments are not retryable.
    key = "dashboard_chat-9-997"
    it2 = _create(key)
    launched2, _ = wi.launch_item(key, it2["id"])
    wi.record_launch_accepted(key, launched2["assignment"]["worker_run_id"])
    wi.record_launch_delivered(key, launched2["assignment"]["worker_run_id"])
    with pytest.raises(wi.WorkItemError, match="eligible"):
        wi.arm_launch_retry(key, it2["id"])


def test_worker_isolation_across_keys_and_coordinators():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    own_key = f"subagent:{run_id}"

    assert [view["id"] for view in wi.worker_assigned_list(own_key)] == [item["id"]]
    assert wi.worker_assigned_list("subagent:deadbeef") == []
    with pytest.raises(wi.WorkItemAssignmentDenied):
        wi.worker_assigned_list("chat-other")
    with pytest.raises(wi.WorkItemAssignmentDenied):
        wi.worker_assigned_read("subagent:deadbeef", item["id"])
    with pytest.raises(wi.WorkItemAssignmentDenied):
        wi.worker_report_progress("chat-other", item["id"], "sneaky", "progress")
    # A well-formed key that was never assigned gets nothing.
    with pytest.raises(wi.WorkItemAssignmentDenied):
        wi.worker_assigned_read("subagent:01234567", item["id"])
    # Another coordinator cannot reach this store's worker surface either.
    with pytest.raises(wi.WorkItemAssignmentDenied):
        wi.worker_assigned_read(own_key, "wi_" + "f" * 32)


def test_worker_progress_and_blocker_events_carry_actor():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    worker_key = f"subagent:{run_id}"
    fingerprint = item["assignment"]["worker_fingerprint"]

    view = wi.worker_report_progress(worker_key, item["id"], "halfway there", "progress")
    assert view["events"][-1]["kind"] == "progress"
    assert view["events"][-1]["actor"] == fingerprint
    view = wi.worker_report_progress(worker_key, item["id"], "stuck on deps", "blocker")
    assert view["events"][-1]["kind"] == "blocker"
    with pytest.raises(wi.WorkItemError, match="progress kind"):
        wi.worker_report_progress(worker_key, item["id"], "nope", "chit-chat")


def test_worker_write_after_revocation_is_stale():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    worker_key = f"subagent:{run_id}"

    wi.revoke_assignment(KEY, item["id"])
    with pytest.raises(wi.WorkItemAssignmentStale):
        wi.worker_report_progress(worker_key, item["id"], "late work", "progress")
    with pytest.raises(wi.WorkItemAssignmentStale):
        wi.worker_submit_handoff(
            worker_key,
            item["id"],
            outcome="too late",
            next_action="nothing",
            verification=["x"],
        )


def test_handoff_validation_pairing_and_bounds():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    worker_key = f"subagent:{run_id}"

    with pytest.raises(wi.WorkItemError, match="outcome"):
        wi.worker_submit_handoff(
            worker_key, item["id"], outcome="", next_action="n", verification=["v"]
        )
    with pytest.raises(wi.WorkItemError, match="verification"):
        wi.worker_submit_handoff(
            worker_key, item["id"], outcome="o", next_action="n", verification=[]
        )
    with pytest.raises(wi.WorkItemError, match="verification"):
        wi.worker_submit_handoff(
            worker_key,
            item["id"],
            outcome="o",
            next_action="n",
            verification=[f"v{i}" for i in range(9)],
        )
    with pytest.raises(wi.WorkItemError, match="both be set"):
        wi.worker_submit_handoff(
            worker_key,
            item["id"],
            outcome="o",
            next_action="n",
            verification=["v"],
            blocker="blocked",
        )


def test_handoff_replaces_latest_and_never_changes_state():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    worker_key = f"subagent:{run_id}"
    fingerprint = item["assignment"]["worker_fingerprint"]

    first = wi.worker_submit_handoff(
        worker_key, item["id"], outcome="first pass", next_action="check", verification=["v1"]
    )
    assert first["state"] == wi.STATE_DISPATCHED
    assert first["worker_handoff"]["actor"] == fingerprint
    assert first["worker_handoff"]["at"]

    second = wi.worker_submit_handoff(
        worker_key,
        item["id"],
        outcome="second pass",
        next_action="review",
        verification=["v2"],
        blocker="flaky test",
        release_condition="pin the fixture",
    )
    assert second["worker_handoff"]["outcome"] == "second pass"
    assert second["worker_handoff"]["blocker"] == "flaky test"
    handoffs = [event for event in second["events"] if event["kind"] == "worker_handoff"]
    assert len(handoffs) == 2
    assert second["state"] == wi.STATE_DISPATCHED
    read = wi.read_item(KEY, item["id"])
    assert read["worker_handoff"]["outcome"] == "second pass"


def test_worker_view_excludes_evaluator_internals():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    wi.worker_report_progress(f"subagent:{run_id}", item["id"], "working", "progress")
    results = wi.evaluate_items(
        KEY, [item["id"]], evaluator=lambda record: ("fail", "not yet")
    )
    assert results[0]["verdict"] == "fail"
    view = wi.worker_assigned_read(f"subagent:{run_id}", item["id"])
    assert "last_evaluation" not in view
    assert "migration_provenance" not in view
    full = wi.read_item(KEY, item["id"])
    assert full["last_evaluation"] is not None


def test_archive_preserves_assignment_and_handoff_evidence():
    item, _ = _launch()
    run_id = item["assignment"]["worker_run_id"]
    wi.record_launch_accepted(KEY, run_id)
    wi.record_launch_delivered(KEY, run_id)
    worker_key = f"subagent:{run_id}"
    wi.worker_report_progress(worker_key, item["id"], "done, needs review", "progress")
    wi.worker_submit_handoff(
        worker_key,
        item["id"],
        outcome="repair complete",
        next_action="verify in staging",
        verification=["test suite green"],
    )

    wi.transition_item(KEY, item["id"], state_name="rejected", event="reviewed and rejected")
    wi.close_cycle(KEY, summary="slice 3 rehearsal")
    archives = wi.list_archives(KEY)
    archive = wi.read_archive(KEY, archives[0]["id"])
    archived_item = archive["cycle"]["items"][0]
    assert archived_item["assignment"]["status"] == wi.ASSIGNMENT_DELIVERED
    assert archived_item["assignment"]["worker_run_id"] == run_id
    assert archived_item["worker_handoff"]["outcome"] == "repair complete"
    kinds = [event["kind"] for event in archived_item["events"]]
    assert "assignment" in kinds and "worker_handoff" in kinds
