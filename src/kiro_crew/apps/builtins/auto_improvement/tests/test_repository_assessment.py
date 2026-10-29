from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from ..backend import assessment_evidence as evidence_module
from ..backend import repository_assessment as assessment
from ..backend import store


@pytest.fixture
def evidence():
    return {
        "revision": "a" * 40,
        "entries": {
            p: ("100644", "oid", 10)
            for p in ("app/service.py", "tests/test_service.py", "docs/perf.md")
        },
        "files": {
            "docs/perf.md": "Run python scripts/bench.py for timing",
            "tests/test_service.py": "from app.service import process",
        },
        "trackedPaths": ["app/service.py", "tests/test_service.py", "docs/perf.md"],
        "truncated": True,
    }


def proposal(kind="test_mapping"):
    return {
        "summary": "Review the mappings",
        "findings": [
            {
                "kind": kind,
                "title": "Service coverage",
                "detail": "The test imports the service",
                "sourcePaths": ["app/service.py"],
                "testPaths": ["tests/test_service.py"],
                "evidencePaths": ["tests/test_service.py"],
            }
        ],
    }


def test_valid_mapping_is_review_only_and_unmeasured(evidence):
    result = evidence_module.validate_result(json.dumps(proposal()), evidence)
    assert result["partial"] and result["reviewOnly"]
    assert result["findings"][0]["measured"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("sourcePaths", ["../private"]),
        ("sourcePaths", ["missing.py"]),
        ("evidencePaths", ["app/service.py"]),
        ("testPaths", []),
        ("testPaths", ["app/service.py"]),
        ("measured", True),
        ("command", "rm -rf /"),
    ],
)
def test_rejects_unfounded_or_unsafe_proposals(evidence, field, value):
    body = proposal()
    body["findings"][0][field] = value
    with pytest.raises(ValueError):
        evidence_module.validate_result(json.dumps(body), evidence)


def test_benchmark_command_must_exist_in_cited_evidence(evidence):
    body = proposal("benchmark_candidate")
    finding = body["findings"][0]
    finding.update(evidencePaths=["docs/perf.md"], command="python scripts/bench.py")
    result = evidence_module.validate_result(json.dumps(body), evidence)
    assert result["reviewOnly"]
    finding["command"] = "python scripts/invented.py"
    with pytest.raises(ValueError, match="not present"):
        evidence_module.validate_result(json.dumps(body), evidence)


def test_rejects_symlink_and_oversized_response(evidence):
    evidence["entries"]["app/service.py"] = ("120000", "oid", 10)
    with pytest.raises(ValueError, match="symlink"):
        evidence_module.validate_result(json.dumps(proposal()), evidence)
    with pytest.raises(ValueError, match="size"):
        evidence_module.validate_result(" " * 48_001, evidence)


def test_prompt_budget_includes_paths(evidence):
    evidence["trackedPaths"] = ["x" * 200_001]
    with pytest.raises(ValueError, match="prompt limit"):
        evidence_module.assessment_prompt(evidence)


@pytest.fixture
def worker(monkeypatch, tmp_path, evidence):
    monkeypatch.setattr(assessment, "_stop", threading.Event())
    monkeypatch.setattr(assessment, "_thread", None)
    monkeypatch.setattr(assessment, "_active_key", "")
    monkeypatch.setattr(assessment, "capture_evidence", lambda *args: evidence)
    identity = {"key": "test-key", "branch": "main", "revision": "a" * 40, "areaPath": ""}
    monkeypatch.setattr(assessment, "_identity", lambda *args: identity)
    notices = []
    monkeypatch.setattr(assessment.notify, "task_finished", lambda *a, **kw: notices.append(a))
    config = {"clone": str(tmp_path), "branch": "main"}
    store.write_json_atomic(store.config_path(), config)
    calls = []

    def run(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return SimpleNamespace(ok=True, text=json.dumps(proposal()))

    monkeypatch.setattr(assessment, "_build_runner", lambda: SimpleNamespace(run=run))
    return identity, config, calls, notices


def test_worker_no_tools_no_repo_cwd_and_does_not_save_config(worker):
    identity, config, calls, notices = worker
    assessment._work({**identity, "state": "assessing"}, config)
    result = store.read_json(assessment._path(identity["key"]))
    assert result["state"] == "ready"
    assert calls[0][1]["allowed_tools"] == []
    assert calls[0][1]["max_turns"] == 1
    assert calls[0][1]["timeout_s"] == 300
    assert calls[0][1]["cwd"] != config["clone"]
    assert store.read_json(store.config_path()) == config
    assert len(notices) == 1


def test_changed_revision_marks_completion_stale(worker, monkeypatch):
    identity, config, _, _ = worker
    monkeypatch.setattr(assessment, "_identity", lambda *args: {"key": "new"})
    assessment._work({**identity, "state": "assessing"}, config)
    assert store.read_json(assessment._path(identity["key"]))["state"] == "stale"


def test_cancel_before_provider_start(worker):
    identity, config, calls, _ = worker
    assessment.stop_assessment()
    assessment._work({**identity, "state": "assessing"}, config)
    assert store.read_json(assessment._path(identity["key"]))["state"] == "cancelled"
    assert calls == []


def test_worker_failure_is_saved_and_notified(worker, monkeypatch):
    identity, config, _, notices = worker

    def fail():
        raise ValueError("Provider unavailable")

    monkeypatch.setattr(assessment, "_build_runner", fail)
    assessment._work({**identity, "state": "assessing"}, config)
    record = store.read_json(assessment._path(identity["key"]))
    assert record["state"] == "failed"
    assert record["error"] == "Provider unavailable"
    assert len(notices) == 1


def test_interrupted_record_and_other_revision_not_reused(worker, monkeypatch):
    identity, _, _, _ = worker
    store.write_json_atomic(assessment._path(identity["key"]), {**identity, "state": "assessing"})
    monkeypatch.setattr(assessment, "assessment_running", lambda: False)
    assert assessment.read_assessment()["state"] == "interrupted"
    monkeypatch.setattr(assessment, "_identity", lambda *args: {"key": "different-revision"})
    assert assessment.read_assessment()["state"] == "none"


def test_other_active_worker_cannot_hide_interrupted_record(worker, monkeypatch):
    identity, _, _, _ = worker
    store.write_json_atomic(assessment._path(identity["key"]), {**identity, "state": "assessing"})
    monkeypatch.setattr(assessment, "assessment_running", lambda: True)
    monkeypatch.setattr(assessment, "_active_key", "another-key")
    assert assessment.read_assessment()["state"] == "interrupted"


def test_background_start_refuses_duplicate_and_accepts_stop(worker, monkeypatch):
    from ..backend import routes

    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)

    def hold(record, config):
        entered.set()
        assert release.wait(30), "Test did not release assessment worker"

    monkeypatch.setattr(assessment, "_work", hold)
    try:
        first = assessment.start_assessment()
        assert entered.wait(30), "Assessment worker did not start"
        assert first["state"] == "assessing"
        assert assessment.start_assessment() is None
        assessment.stop_assessment()
        assert assessment._stop.is_set()
    finally:
        release.set()
        if assessment._thread is not None:
            assessment._thread.join(30)
            assert not assessment._thread.is_alive(), "Assessment worker did not finish"
