"""Persistence tests for Design Critique review runs."""

from __future__ import annotations

import json

from kiro_crew.apps.builtins.design_critique.backend import routes


def test_create_and_complete_review_run(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "app_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(routes, "redact_exfiltration_urls", lambda text: (text, []))
    monkeypatch.setattr(routes, "redact_credentials", lambda text: (text, []))

    run = routes._create_run(
        {
            "slot_key": "dc-1",
            "agent": "crew-codex",
            "model": "auto",
            "stage": "analyzing",
            "source": {"kind": "screenshots"},
            "screens": [{"step": 1, "label": "Screen 1", "url": "/api/file-raw?path=one.png"}],
        }
    )
    completed = routes._update_run(
        run["id"],
        {"status": "completed", "stage": "report", "report": {"overallRead": "Ready"}},
    )

    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["report"] == {"overallRead": "Ready"}
    stored = json.loads((tmp_path / "review-runs.json").read_text(encoding="utf-8"))
    assert stored == [completed]


def test_recovery_marks_unfinished_runs_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "app_data_dir", lambda _name: tmp_path)
    (tmp_path / "review-runs.json").write_text(
        json.dumps([{"id": "run-1", "status": "running"}, {"id": "run-2", "status": "completed"}]),
        encoding="utf-8",
    )

    routes._recover_runs()

    runs = routes._list_runs()
    assert runs[0]["status"] == "interrupted"
    assert runs[0]["error"] == {"code": "gateway_restarted"}
    assert runs[1]["status"] == "completed"
