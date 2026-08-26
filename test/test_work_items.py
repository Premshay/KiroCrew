"""Coordinator work-item storage and closed-cycle archive contracts."""

from __future__ import annotations

import json

import pytest

from kiro_crew import session_ledger, work_items as wi


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _open(key: str = "dashboard_chat-1-111") -> dict:
    return wi.open_cycle(key, goal="Coordinate a focused repair", next_action="Create work items")


def _create(key: str = "dashboard_chat-1-111", **overrides) -> dict:
    _open(key)
    return wi.create_item(
        key,
        title=overrides.get("title", "Repair the endpoint"),
        acceptance=overrides.get(
            "acceptance", {"kind": "file", "path": "/tmp/proof", "exists": True}
        ),
        next_action=overrides.get("next_action", "Implement and verify the repair"),
        canonical_ref=overrides.get("canonical_ref", ""),
        declared_resources=overrides.get("declared_resources", ["src/example.py"]),
    )


def test_distinct_channel_keys_have_distinct_coordinator_stores():
    a = wi.coordinator_dir("wecom:agent:direct:user_gen1")
    b = wi.coordinator_dir("wecom:agent:direct:user:gen1")
    assert a != b


def test_active_cycle_and_item_survive_a_fresh_read():
    key = "dashboard_chat-2-222"
    cycle = _open(key)
    item = wi.create_item(
        key,
        title="Check CI",
        acceptance={"kind": "pr_checks", "repo": "owner/repo", "pr": 12},
        next_action="Wait for checks",
    )

    persisted = wi.active_cycle("chat-2-222")
    assert persisted is not None
    assert persisted["id"] == cycle["id"]
    assert persisted["items"][0]["id"] == item["id"]
    assert persisted["items"][0]["acceptance"] == {
        "kind": "pr_checks",
        "repo": "owner/repo",
        "pr": 12,
    }


def test_create_refuses_command_shaped_acceptance():
    _open()
    with pytest.raises(wi.WorkItemError, match="may not name a command"):
        wi.create_item(
            "dashboard_chat-1-111",
            title="Run a command",
            acceptance={"kind": "cmd", "argv": ["git", "reset", "--hard"]},
            next_action="Never run it",
        )


def test_update_cannot_replace_acceptance_or_state():
    item = _create()
    updated = wi.update_item(
        "dashboard_chat-1-111",
        item["id"],
        canonical_ref="https://example.test/pr/1",
        event="Published the pull request",
    )
    assert updated["state"] == wi.STATE_PROPOSED
    assert updated["acceptance"] == item["acceptance"]
    assert updated["canonical_ref"].endswith("/1")

    with pytest.raises(TypeError):
        wi.update_item(  # type: ignore[call-arg]
            "dashboard_chat-1-111", item["id"], acceptance={"kind": "human_approval"}
        )


def test_only_evaluator_pass_transitions_to_accepted():
    item = _create()
    with pytest.raises(wi.WorkItemError, match="may not transition"):
        wi.transition_item(
            "dashboard_chat-1-111",
            item["id"],
            state_name=wi.STATE_ACCEPTED,
            event="self-certified",
        )

    results = wi.evaluate_items(
        "dashboard_chat-1-111",
        [item["id"]],
        evaluator=lambda _item: ("pass", "required file exists"),
    )
    assert results == [{"id": item["id"], "verdict": "pass", "evidence": "required file exists"}]
    assert wi.read_item("dashboard_chat-1-111", item["id"])["state"] == wi.STATE_ACCEPTED


def test_failed_evaluation_records_evidence_without_rejecting_item():
    item = _create()
    wi.evaluate_items(
        "dashboard_chat-1-111",
        [item["id"]],
        evaluator=lambda _item: ("fail", "check is red"),
    )
    current = wi.read_item("dashboard_chat-1-111", item["id"])
    assert current["state"] == wi.STATE_PROPOSED
    assert current["last_evaluation"] == {
        "at": current["last_evaluation"]["at"],
        "verdict": "fail",
        "evidence": "check is red",
    }


def test_evaluator_error_is_recorded_without_hiding_a_sibling_pass():
    key = "dashboard_chat-1-111"
    first = _create(key, title="First")
    second = wi.create_item(
        key,
        title="Second",
        acceptance={"kind": "file", "path": "/tmp/second", "exists": True},
        next_action="Evaluate the second item",
    )

    def _evaluator(item):
        if item["id"] == first["id"]:
            raise RuntimeError("fixed evaluator unavailable")
        return "pass", "second condition passed"

    results = wi.evaluate_items(key, [first["id"], second["id"]], evaluator=_evaluator)
    assert results[0]["verdict"] == "error"
    assert results[1]["verdict"] == "pass"
    assert wi.read_item(key, first["id"])["state"] == wi.STATE_PROPOSED
    assert wi.read_item(key, second["id"])["state"] == wi.STATE_ACCEPTED


def test_close_requires_terminal_items_and_archives_cycle():
    key = "dashboard_chat-3-333"
    item = _create(key)
    with pytest.raises(wi.WorkItemError, match="must be terminal"):
        wi.close_cycle(key, summary="not done")

    wi.transition_item(key, item["id"], state_name=wi.STATE_CANCELLED, event="out of scope")
    archived = wi.close_cycle(key, summary="Cancelled the only item after review")
    assert wi.active_cycle(key) is None
    assert archived["id"].startswith("wc_")
    assert archived["item_states"] == {item["id"]: wi.STATE_CANCELLED}

    history = wi.list_archives(key)
    assert history == [archived]
    whole = wi.read_archive(key, archived["id"])
    assert whole["summary"] == "Cancelled the only item after review"
    assert whole["cycle"]["items"][0]["id"] == item["id"]


def test_close_retry_recovers_archive_written_before_state_clear(monkeypatch):
    key = "dashboard_chat-4-444"
    item = _create(key)
    wi.transition_item(key, item["id"], state_name=wi.STATE_REJECTED, event="not viable")
    original_write = wi._write_state

    def _interrupted(*args, **kwargs):
        raise OSError("simulated crash before active state replacement")

    monkeypatch.setattr(wi, "_write_state", _interrupted)
    with pytest.raises(OSError, match="simulated crash"):
        wi.close_cycle(key, summary="Rejected after review")
    assert wi.active_cycle(key) is not None
    assert len(wi.list_archives(key)) == 1

    monkeypatch.setattr(wi, "_write_state", original_write)
    closed = wi.close_cycle(key, summary="retry has different prose but same cycle")
    assert wi.active_cycle(key) is None
    assert len(wi.list_archives(key)) == 1
    assert closed["id"].startswith("wc_")


def test_full_archive_refuses_close_without_clearing_the_terminal_cycle(monkeypatch):
    key = "dashboard_chat-4-445"
    item = _create(key)
    wi.transition_item(key, item["id"], state_name=wi.STATE_CANCELLED, event="out of scope")
    monkeypatch.setattr(wi, "_MAX_ARCHIVES", 0)
    with pytest.raises(wi.WorkItemArchiveFull, match="archive is full"):
        wi.close_cycle(key, summary="must retain this active cycle")
    assert wi.active_cycle(key) is not None


def test_corrupt_existing_state_is_not_treated_as_an_empty_store():
    key = "dashboard_chat-5-555"
    _open(key)
    wi._state_path(wi.coordinator_dir(key)).write_text("{not JSON", encoding="utf-8")
    with pytest.raises(wi.WorkItemStoreCorrupt, match="unreadable"):
        wi.active_cycle(key)


def test_corrupt_persisted_acceptance_is_not_a_client_validation_error():
    key = "dashboard_chat-5-556"
    item = _create(key)
    path = wi._state_path(wi.coordinator_dir(key))
    state = json.loads(path.read_text(encoding="utf-8"))
    state["active_cycle"]["items"][0]["acceptance"] = {"kind": "cmd", "argv": ["x"]}
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(wi.WorkItemStoreCorrupt, match="immutable"):
        wi.read_item(key, item["id"])


def test_purge_matching_removes_active_and_archived_state():
    key = "dashboard_chat-6-666"
    item = _create(key)
    wi.transition_item(key, item["id"], state_name=wi.STATE_CANCELLED, event="finished")
    wi.close_cycle(key, summary="finished")
    dir_path = wi.coordinator_dir(key)
    assert dir_path.exists()

    removed = wi.purge_matching({wi.ledger_key(key)}, {"chat-6-666"}, lambda value: value)
    assert removed == 1
    assert not dir_path.exists()


def test_legacy_running_item_imports_once_as_waiting_with_provenance():
    key = "dashboard_chat-7-777"
    session_ledger.record(
        session_ledger.ledger_key(key),
        artifacts={
            "item-1": json.dumps(
                {
                    "accept": {"kind": "file", "path": "/tmp/proof", "exists": True},
                    "session": "child-session-1",
                    "round": 3,
                    "since": "cursor-9",
                    "status": "running",
                }
            )
        },
    )

    state = wi.read_state(key)
    item = state["active_cycle"]["items"][0]
    assert item["state"] == wi.STATE_WAITING
    assert item["migration_provenance"] == {
        "legacy_key": "item-1",
        "session": "child-session-1",
        "status": "running",
        "since": "cursor-9",
        "round": 3,
    }
    assert "child-session-1" in item["next_action"]

    # Re-reading after the marker is complete cannot create a duplicate item.
    assert len(wi.read_state(key)["active_cycle"]["items"]) == 1


def test_legacy_terminal_items_become_one_closed_archive_and_bad_values_warn():
    key = "dashboard_chat-8-888"
    session_ledger.record(
        session_ledger.ledger_key(key),
        artifacts={
            "item-1": json.dumps(
                {
                    "accept": {"kind": "human_approval"},
                    "session": "child-terminal",
                    "round": 1,
                    "status": "pass",
                }
            ),
            "item-2": "not-json",
        },
    )

    # Archive listing can be the first upgraded call.  It must trigger the
    # documented one-time migration rather than misleading a coordinator with
    # an empty history until some unrelated read occurs.
    archives = wi.list_archives(key)
    assert len(archives) == 1
    archive = wi.read_archive(key, archives[0]["id"])
    assert archive["cycle"]["items"][0]["state"] == wi.STATE_ACCEPTED

    state = wi.read_state(key)
    assert state["active_cycle"] is None
    assert state["migration"]["completed"] is True
    assert state["migration"]["warnings"] == ["item-2: legacy value is not valid JSON"]


@pytest.mark.asyncio
async def test_permanent_history_delete_purges_active_and_archived_work_items():
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers.sessions import _remove_slot_for_history_key

    key = "dashboard_chat-9-999"
    item = _create(key)
    wi.transition_item(key, item["id"], state_name=wi.STATE_CANCELLED, event="finished")
    wi.close_cycle(key, summary="archived before history delete")
    assert wi.coordinator_dir(key).exists()

    state = MagicMock()
    state._slots = {}
    state.crew = None
    state.remove_chat_pins_for_slots = AsyncMock()
    await _remove_slot_for_history_key(state, key)
    assert not wi.coordinator_dir(key).exists()
