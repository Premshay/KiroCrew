"""Private golden-set measurement for Knowledge Library retrieval."""

from __future__ import annotations

import json

import pytest

from kiro_crew.knowledge.evaluation import (
    GoldenSetError,
    evaluate_golden_set,
    load_golden_set,
    report_summary,
    resolve_private_eval_path,
    write_report,
)
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    value = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield value
    value.close()


def _write_golden(tmp_path, cases):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"version": 1, "name": "test-set", "cases": cases}))
    return load_golden_set(path)


def test_measures_answerable_abstention_and_source_scope(store, tmp_path):
    source_a = store.add_source("project-a", "local_file", "/private/project-a.md")
    source_b = store.add_source("project-b", "local_file", "/private/project-b.md")
    item_a = store.add_item(
        "Release window",
        "The release window is Tuesday at 09:00 UTC.",
        "document",
        source_id=source_a,
    )
    item_b = store.add_item(
        "Different project",
        "The release window is Tuesday at 09:00 UTC.",
        "document",
        source_id=source_b,
    )
    store.add_source_location(item_a, source_a, section_title="Approved window")
    store.add_source_location(item_b, source_b, section_title="Different window")
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "scoped-release-window",
                "query": "release window Tuesday",
                "category": "clean-fact",
                "scope_source_uri": "/private/project-a.md",
                "expected": [
                    {
                        "source_uri": "/private/project-a.md",
                        "section_title": "Approved window",
                    }
                ],
            },
            {
                "id": "absence",
                "query": "QZZV-ABSENT-RETRIEVAL-CASE-903",
                "category": "abstention",
                "expected": [],
            },
        ],
    )

    report = evaluate_golden_set(store, golden, limit=5, embedding_signature="test-embedder")

    assert report["metrics"] == {
        "answerable_cases": 1,
        "abstention_cases": 1,
        "hit_at_k": 1.0,
        "recall_at_k": 1.0,
        "mrr": 1.0,
        "ndcg_at_k": 1.0,
        "abstention_accuracy": 1.0,
        "scope_leak_rate": 0.0,
        "p50_latency_ms": pytest.approx(report["metrics"]["p50_latency_ms"]),
        "p95_latency_ms": pytest.approx(report["metrics"]["p95_latency_ms"]),
    }
    assert report["config"]["embedding_signature"] == "test-embedder"
    assert report["cases"][0]["results"][0]["source_uri"] == "/private/project-a.md"
    assert not report["cases"][0]["scope_leak"]
    assert "hit@5=1.000" in report_summary(report)


def test_matches_a_deduplicated_document_by_its_non_owner_source(store, tmp_path):
    owner = store.add_source("owner", "local_file", "/private/owner.md")
    holder = store.add_source("holder", "local_file", "/private/holder.md")
    item = store.add_item(
        "Shared decision", "The shared retention period is 30 days.", "document", source_id=owner
    )
    store.add_source_location(item, holder, section_title="Retention")
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "shared-holder",
                "query": "shared retention period",
                "category": "source-scope",
                "scope_source_uri": "/private/holder.md",
                "expected": [
                    {
                        "source_uri": "/private/holder.md",
                        "content_contains": "retention period is 30 days",
                    }
                ],
            }
        ],
    )

    report = evaluate_golden_set(store, golden)

    assert report["metrics"]["hit_at_k"] == 1.0
    assert report["metrics"]["scope_leak_rate"] == 0.0


def test_rejects_an_expected_locator_without_a_stable_document_anchor(tmp_path):
    path = tmp_path / "golden.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "test-set",
                "cases": [
                    {
                        "id": "too-broad",
                        "query": "release",
                        "category": "clean-fact",
                        "expected": [{"source_uri": "/private/project-a.md"}],
                    }
                ],
            }
        )
    )

    with pytest.raises(GoldenSetError, match="needs a file, section, or excerpt anchor"):
        load_golden_set(path)


def test_rejects_fixture_that_names_a_source_no_longer_in_the_library(store, tmp_path):
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "missing-source",
                "query": "release",
                "category": "clean-fact",
                "expected": [
                    {
                        "source_uri": "/private/missing.md",
                        "section_title": "Decision",
                    }
                ],
            }
        ],
    )

    with pytest.raises(GoldenSetError, match="expected source is not registered"):
        evaluate_golden_set(store, golden)


def test_private_paths_cannot_escape_evaluation_directory_and_report_is_atomic(tmp_path):
    root = tmp_path / "data-home"
    with pytest.raises(GoldenSetError, match="private eval directory"):
        resolve_private_eval_path(root, "../outside.json")

    report = {"metrics": {}, "config": {"limit": 5}}
    directory = root / "workspace" / "knowledge" / "evals"
    destination = write_report(report, directory, stem="baseline")

    assert destination == directory / "baseline.json"
    assert json.loads(destination.read_text()) == report
    assert not destination.with_suffix(".json.tmp").exists()

    with pytest.raises(GoldenSetError, match="must not contain a path"):
        write_report(report, directory, stem="../outside")
