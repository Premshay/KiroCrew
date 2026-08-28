"""Launched-subagent dispatch adapter against the governed manager surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import types

import pytest

from kiro_crew import work_dispatch as wd
from kiro_crew import work_items as wi

KEY = "dashboard_chat-9-999"
AGENT = "crew-codex"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


class SpawnInfo:
    def __init__(self, error=None, outcome=""):
        self.error = error
        self.outcome = outcome
        self.parent_session_key = ""
        self.id = ""


class FakeManager:
    def __init__(self, behavior: str = "accepted"):
        self.behavior = behavior
        self.hooks: list = []
        self.spawned: list[dict] = []
        self.run_states: dict[str, str] = {}
        self.cancelled: list[str] = []

    def add_event_hook(self, hook) -> None:
        if hook not in self.hooks:
            self.hooks.append(hook)

    def spawn(self, contract, parent_session_key="", agent="", _preassigned_id=""):
        self.spawned.append(
            {
                "contract": contract,
                "parent_session_key": parent_session_key,
                "agent": agent,
                "run_id": _preassigned_id,
            }
        )
        if self.behavior == "raise":
            raise RuntimeError("at capacity: too many concurrent subagents")
        if self.behavior == "capacity":
            return SpawnInfo(error="at capacity: too many concurrent subagents")
        if self.behavior == "refused":
            return SpawnInfo(error="spawn refused by launch policy")
        if self.behavior == "none":
            return None
        self.run_states[_preassigned_id] = "queued"
        return SpawnInfo()

    def run_state(self, run_id: str) -> str:
        return self.run_states.get(run_id, "unknown")

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def fire(self, etype: str, run_id: str, outcome: str = "completed") -> None:
        info = SpawnInfo(outcome=outcome)
        info.parent_session_key = KEY
        info.id = run_id
        extra = {"outcome": outcome} if etype == "subagent_done" else None
        for hook in self.hooks:
            result = hook(etype, info, extra)
            if asyncio.iscoroutine(result):
                await result


@pytest.fixture()
def manager(monkeypatch) -> FakeManager:
    roster = [types.SimpleNamespace(name=AGENT), types.SimpleNamespace(name="kirocrew")]
    monkeypatch.setattr(wd, "list_agents", lambda: list(roster))
    return FakeManager()


def _setup(key: str = KEY) -> dict:
    wi.open_cycle(key, goal="Coordinate a focused repair", next_action="Launch a worker")
    return wi.create_item(
        key,
        title="Repair the endpoint",
        acceptance={"kind": "file", "path": "/tmp/proof", "exists": True},
        next_action="Implement and verify the repair",
    )


def _candidate() -> str:
    return wd.launch_candidates()[0]["id"]


def _launch(manager: FakeManager, item: dict) -> dict:
    return asyncio.run(wd.launch(KEY, item["id"], _candidate(), manager))


def _crash_back_to_pending(status: str) -> None:
    """Rewrite the store as if the gateway died before a receipt was persisted."""
    state_path = wi.coordinator_dir(KEY) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["active_cycle"]["items"][0]["assignment"]["status"] = status
    state_path.write_text(json.dumps(state), encoding="utf-8")


def test_candidates_are_stable_handles_excluding_unadvertised(manager):
    candidates = wd.launch_candidates()
    assert [c["agent"] for c in candidates] == [AGENT]
    expected = hashlib.sha256(AGENT.encode("utf-8")).hexdigest()[:16]
    assert candidates[0]["id"] == expected


def test_launch_unknown_candidate_raises_worker_target_unavailable(manager):
    item = _setup()
    with pytest.raises(wd.WorkDispatchError) as exc:
        asyncio.run(wd.launch(KEY, item["id"], "f" * 16, manager))
    assert exc.value.code == "worker_target_unavailable"
    assert exc.value.status == 400
    assert manager.spawned == []
    assert wi.read_item(KEY, item["id"])["assignment"] is None


def test_launch_capacity_refusal_fails_assignment_and_keeps_item_open(manager):
    manager.behavior = "capacity"
    item = _setup()
    result = _launch(manager, item)
    assert result["code"] == "dispatch_delivery_failed"
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_FAILED
    assert read["assignment"]["failure_code"] == "launch_capacity"
    assert read["state"] == wi.STATE_PROPOSED


def test_launch_spawn_exception_is_502_and_classified(manager):
    manager.behavior = "raise"
    item = _setup()
    with pytest.raises(wd.WorkDispatchError) as exc:
        _launch(manager, item)
    assert exc.value.status == 502
    assert exc.value.code == "launch_capacity"
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_FAILED
    assert read["assignment"]["failure_code"] == "launch_capacity"
    assert read["state"] == wi.STATE_PROPOSED


def test_launch_none_receipt_is_worker_launch_refused(manager):
    manager.behavior = "none"
    item = _setup()
    result = _launch(manager, item)
    assert result["code"] == "dispatch_delivery_failed"
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["failure_code"] == "worker_launch_refused"


def test_launch_accepted_is_queued_until_child_start(manager):
    item = _setup()
    result = _launch(manager, item)
    assert result["code"] == "launch_queued"
    assignment = result["item"]["assignment"]
    run_id = assignment["worker_run_id"]
    assert assignment["status"] == wi.ASSIGNMENT_LAUNCH_QUEUED
    assert result["item"]["state"] == wi.STATE_PROPOSED
    spawn = manager.spawned[0]
    assert spawn["agent"] == AGENT
    assert spawn["parent_session_key"] == KEY
    assert spawn["run_id"] == run_id
    assert item["title"] in spawn["contract"]

    asyncio.run(manager.fire("subagent_spawn", run_id))
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_DELIVERED
    assert read["state"] == wi.STATE_DISPATCHED
    events = read["events"]

    # A duplicate child-start receipt is a silent no-op.
    asyncio.run(manager.fire("subagent_spawn", run_id))
    again = wi.read_item(KEY, item["id"])
    assert again["events"] == events
    assert again["assignment"]["status"] == wi.ASSIGNMENT_DELIVERED


def test_child_done_records_runtime_event_and_keeps_item_dispatched(manager):
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    asyncio.run(manager.fire("subagent_spawn", run_id))
    asyncio.run(manager.fire("subagent_done", run_id, outcome="completed"))
    read = wi.read_item(KEY, item["id"])
    assert read["state"] == wi.STATE_DISPATCHED
    assert read["events"][-1]["kind"] == "runtime"
    assert "child run ended: completed" in read["events"][-1]["text"]


def test_retry_of_unknown_run_resubmits_the_same_run_id(manager):
    manager.behavior = "capacity"
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    assert manager.run_state(run_id) == "unknown"

    manager.behavior = "accepted"
    result = asyncio.run(wd.dispatch_retry(KEY, item["id"], manager))
    assert result["code"] == "launch_queued"
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["attempt"] == 2
    assert read["assignment"]["status"] == wi.ASSIGNMENT_LAUNCH_QUEUED
    assert [spawn["run_id"] for spawn in manager.spawned] == [run_id, run_id]
    assert manager.spawned[1]["agent"] == AGENT


def test_retry_of_queued_run_is_noop_without_duplicate_spawn(manager):
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    # The gateway died between manager acceptance and the acceptance receipt.
    _crash_back_to_pending(wi.ASSIGNMENT_PENDING_DELIVERY)

    result = asyncio.run(wd.dispatch_retry(KEY, item["id"], manager))
    assert result["code"] == "launch_queued"
    assert len(manager.spawned) == 1
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_LAUNCH_QUEUED


def test_retry_of_running_run_recovers_to_dispatched(manager):
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    manager.run_states[run_id] = "running"
    # The child-start receipt was lost with the gateway.
    _crash_back_to_pending(wi.ASSIGNMENT_PENDING_DELIVERY)

    result = asyncio.run(wd.dispatch_retry(KEY, item["id"], manager))
    assert result["code"] == "dispatched"
    assert len(manager.spawned) == 1
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_DELIVERED
    assert read["state"] == wi.STATE_DISPATCHED


def test_retry_of_terminal_run_is_refused(manager):
    manager.behavior = "capacity"
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    manager.run_states[run_id] = "terminal"

    with pytest.raises(wd.WorkDispatchError) as exc:
        asyncio.run(wd.dispatch_retry(KEY, item["id"], manager))
    assert exc.value.code == "worker_launch_refused"
    assert exc.value.status == 409
    assert len(manager.spawned) == 1


def test_retry_of_delivered_assignment_is_a_noop(manager):
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    asyncio.run(manager.fire("subagent_spawn", run_id))

    result = asyncio.run(wd.dispatch_retry(KEY, item["id"], manager))
    assert result["code"] == "dispatched"
    assert result["item"]["state"] == wi.STATE_DISPATCHED
    assert len(manager.spawned) == 1


def test_retry_of_revoked_assignment_is_stale(manager):
    item = _setup()
    _launch(manager, item)
    wi.revoke_assignment(KEY, item["id"])

    with pytest.raises(wd.WorkDispatchError) as exc:
        asyncio.run(wd.dispatch_retry(KEY, item["id"], manager))
    assert exc.value.code == "assignment_stale"
    assert exc.value.status == 409


def test_retry_without_assignment_is_worker_target_unavailable(manager):
    item = _setup()
    with pytest.raises(wd.WorkDispatchError) as exc:
        asyncio.run(wd.dispatch_retry(KEY, item["id"], manager))
    assert exc.value.code == "worker_target_unavailable"
    assert exc.value.status == 400


def test_revoke_queued_cancels_the_exact_run(manager):
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    assert manager.run_state(run_id) == "queued"

    result = asyncio.run(wd.revoke_assignment(KEY, item["id"], manager))
    assert result["code"] == "revoked"
    assert manager.cancelled == [run_id]
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_REVOKED
    assert read["state"] == wi.STATE_PROPOSED
    with pytest.raises(wi.WorkItemError, match="already revoked"):
        asyncio.run(wd.revoke_assignment(KEY, item["id"], manager))


def test_revoke_running_run_records_without_killing_it(manager):
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]
    manager.run_states[run_id] = "running"

    result = asyncio.run(wd.revoke_assignment(KEY, item["id"], manager))
    assert result["code"] == "revoked"
    assert manager.cancelled == []
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_REVOKED
    assert read["state"] == wi.STATE_PROPOSED


def test_event_hook_ignores_unrelated_subagents(manager):
    item = _setup()
    _launch(manager, item)
    run_id = wi.read_item(KEY, item["id"])["assignment"]["worker_run_id"]

    # An unrelated child must not deliver this item's contract.
    asyncio.run(manager.fire("subagent_spawn", "01234567"))
    asyncio.run(manager.fire("subagent_done", "01234567", outcome="completed"))
    read = wi.read_item(KEY, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_LAUNCH_QUEUED
    assert read["state"] == wi.STATE_PROPOSED
    assert not [e for e in read["events"] if e["kind"] == "runtime"]
