from __future__ import annotations

import json
import re

import pytest
from sage_lib import results
from sage_lib import review_driver as D
from sage_lib import store


def _record(change_id):
    return {
        "schema": "code-review-sage-result",
        "version": 1,
        "change_id": change_id,
        "platform": "github",
        "repo_identity": "github.com/o/r",
        "revision": "1",
        "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
        "blast_radius": {"rating": "SMALL", "signals": {}},
        "counts": {"red": 0, "yellow": 0},
        "findings": [],
        "deep_reviewed": True,
        "files_covered": ["f"],
        "coverage_complete": True,
    }


def _response(task, change_id):
    match = re.search(r'"capability": "([^"]+)"', task)
    assert match is not None, "task lacks a capability field"
    capability = match.group(1)
    return json.dumps(
        {
            "schema": D._RESPONSE_SCHEMA,
            "version": D._RESPONSE_VERSION,
            "capability": capability,
            "change_id": change_id,
            "record": _record(change_id),
        }
    )


def test_envelope_after_many_narration_braces_decodes():
    response = "narration { probe\n" * (D._MAX_ENVELOPE_CANDIDATES * 2)
    response += _response('"capability": "issued-token"', "CR-1")
    envelope = D._decode_worker_envelope(response, "CR-1", "issued-token")
    assert envelope is not None
    assert envelope["record"] == _record("CR-1")


def test_brace_heavy_finding_survives_envelope_extraction(tmp_path):
    snippet = " ".join(["{}"] * D._MAX_ENVELOPE_CANDIDATES) + ' "quoted" \\ {}'
    record = _record("CR-1")
    record["findings"] = [{"severity": "red", "snippet": snippet}]
    record["counts"] = {"red": 1, "yellow": 0}
    response = json.loads(_response('"capability": "issued-token"', "CR-1"))
    response["record"] = record
    root = tmp_path / "app"

    accepted, error = D._persist_worker_response(
        "```json\n" + json.dumps(response) + "\n```",
        change_id="CR-1",
        capability="issued-token",
        root=root,
        run_id="brace-heavy",
    )

    assert error == ""
    assert accepted == record
    assert results.read_result("CR-1", root, "brace-heavy") == record


def test_candidate_budget_counts_complete_objects():
    response = _response('"capability": "issued-token"', "CR-1")
    suffix = '\n{"unrelated": {"snippet": "' + "{}" * 64 + '"}}'
    within_budget = response + suffix * (D._MAX_ENVELOPE_CANDIDATES - 1)
    envelope = D._decode_worker_envelope(within_budget, "CR-1", "issued-token")
    assert envelope is not None
    assert envelope["record"] == _record("CR-1")
    beyond_budget = D._decode_worker_envelope(within_budget + suffix, "CR-1", "issued-token")
    assert beyond_budget is not None
    assert "record" not in beyond_budget


def test_poster_restore_failure_does_not_abort_batch(tmp_path):
    first = "https://github.com/o/r/pull/1"
    second = "https://github.com/o/r/pull/2"
    first_id = D._cid(first)
    second_id = D._cid(second)
    root = tmp_path / "app"
    run_id = "restore-failure"
    calls = []

    def dispatch(task, timeout):
        if "SINGLE thorough pass" in task:
            change_id = first_id if first in task else second_id
            return {"ok": True, "output": _response(task, change_id), "error": ""}
        calls.append(task)
        if first in task:
            path = results.result_path(first_id, root, run_id)
            path.unlink()
            path.mkdir()
        return {"ok": True, "output": "done", "error": ""}

    out = D.run_review(
        [first, second],
        dispatch=dispatch,
        root=root,
        run_id=run_id,
        post=True,
        concurrency=1,
        confirm=lambda *_: "confirmed-draft",
        archiver=lambda *_: None,
        generate_report=True,
    )
    assert len(calls) == 2
    failed, succeeded = out["per_change"]
    assert failed["post_ok"] is False
    assert "could not restore accepted review record" in failed["post_error"]
    assert succeeded["post_ok"] is True
    report = store.run_dir(run_id, root) / "report" / "report.json"
    assert len(json.loads(report.read_text(encoding="utf-8"))["rows"]) == 2


class TestAcceptedFindingsSurvivePoster:
    @pytest.mark.parametrize("run_id", [None, "poster-substitution"])
    def test_failed_confirmation_keeps_worker_findings_for_manual_retry(self, tmp_path, run_id):
        link = "https://github.com/o/r/pull/1"
        change_id = D._cid(link)
        root = tmp_path / "app"
        accepted = _record(change_id)
        accepted["findings"] = [
            {
                "dimension": "correctness",
                "severity": "red",
                "file": "f",
                "line": 1,
                "snippet": "x",
                "observation": "WORKER-FINDING",
                "consequence": "c",
                "suggestion": "s",
            }
        ]
        accepted["counts"] = {"red": 1, "yellow": 0}
        substituted = json.loads(json.dumps(accepted))
        substituted["findings"][0]["observation"] = "POSTER-FINDING"
        substituted["posted_comments"] = 1
        substituted["design_comment_posted"] = False
        archived, staged_payloads = [], []

        def dispatch(task, timeout):
            if "SINGLE thorough pass" in task:
                response = json.loads(_response(task, change_id))
                response["record"] = accepted
                return {"ok": True, "output": json.dumps(response), "error": ""}
            staged = results.read_result(change_id, root)
            staged_payloads.append(staged["github_review_payload"])
            results.write_result(substituted, root)
            return {"ok": True, "output": "done", "error": ""}

        out = D.run_review(
            [link],
            dispatch=dispatch,
            root=root,
            run_id=run_id,
            post=True,
            concurrency=1,
            confirm=lambda *_: "",
            archiver=lambda html, *_: archived.append(html),
        )
        assert out["per_change"][0]["post_ok"] is False
        assert "WORKER-FINDING" in archived[0]
        assert "POSTER-FINDING" not in archived[0]

        def retry_dispatch(task, timeout):
            staged = results.read_result(change_id, root)
            staged_payloads.append(staged["github_review_payload"])
            return {"ok": True, "output": "done", "error": ""}

        retry = D.post_recorded(
            change_id,
            link,
            dispatch=retry_dispatch,
            root=root,
            run_id=run_id,
            confirm=lambda *_: "confirmed-retry",
        )
        assert retry["post_ok"] is True
        assert len(staged_payloads) == 2
        for payload in staged_payloads:
            assert "WORKER-FINDING" in json.dumps(payload)
            assert "POSTER-FINDING" not in json.dumps(payload)
        durable = results.read_result(change_id, root, run_id)
        assert durable["findings"] == accepted["findings"]
        assert durable["posted_keys"]
