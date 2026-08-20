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


def test_project_context_keeps_repository_and_supporting_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "app_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(routes, "redact_exfiltration_urls", lambda text: (text, []))
    monkeypatch.setattr(routes, "redact_credentials", lambda text: (text, []))

    context = routes._create_context(
        {
            "name": "Atlas",
            "repository": "/work/atlas",
            "context_paths": ["AGENTS.md", "docs/design-system.md"],
            "notes": "Keep the outcome model visible to operators.",
        }
    )
    updated = routes._update_context(context["id"], {"notes": "Review any workflow."})

    assert updated is not None
    assert updated["repository"] == "/work/atlas"
    assert updated["context_paths"] == ["AGENTS.md", "docs/design-system.md"]
    assert updated["notes"] == "Review any workflow."
    assert routes._delete_context(context["id"])
    assert routes._list_contexts() == []


def test_design_round_compiles_grounded_prompt_and_harvest_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "app_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(routes, "redact_exfiltration_urls", lambda text: (text, []))
    monkeypatch.setattr(routes, "redact_credentials", lambda text: (text, []))

    design_round = routes._create_design_round(
        {
            "mode": "generate-prototype",
            "intent": "ground",
            "project_name": "Atlas",
            "repository": "/work/atlas",
            "context_paths": "docs/design-system.md\nwebsite/src/Queue.tsx",
            "notes": "Keep outcome states explicit.",
            "target": "The operator review queue and its state transitions.",
            "claude_design_url": "https://claude.ai/design/p/atlas-project",
            "handoff_path": "docs/design/handoffs/review-queue",
            "review_run_id": "review-1",
            "report": {"findings": [{"title": "Status is unclear", "fix": "Show the active state."}]},
        }
    )
    updated = routes._update_design_round(
        design_round["id"],
        {
            "status": "harvested",
            "evidence": {
                "files": ["artifact.html", "design-system-delta.md", "handoff-bundle-README.md"],
                "note": "Bundle checked into the handoff directory.",
            },
        },
    )

    assert "discover / ground" in design_round["prompt"]
    assert "website/src/Queue.tsx" in design_round["prompt"]
    assert "design-system-delta.md" in design_round["prompt"]
    assert updated is not None
    assert updated["status"] == "harvested"
    assert updated["evidence"]["files"][-1] == "handoff-bundle-README.md"


def test_design_round_rejects_non_claude_design_url(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "app_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(routes, "redact_exfiltration_urls", lambda text: (text, []))
    monkeypatch.setattr(routes, "redact_credentials", lambda text: (text, []))

    try:
        routes._create_design_round({"claude_design_url": "https://example.com/design/p/nope"})
    except ValueError as exc:
        assert str(exc) == "invalid_claude_design_url"
    else:
        raise AssertionError("the round accepted a non-Claude Design URL")
