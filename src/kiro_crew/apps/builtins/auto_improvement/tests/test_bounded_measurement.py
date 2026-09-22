from __future__ import annotations

import json
import logging
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from ..backend import commit, routes, store
from ..profiles.github_repo import profile as gp
from ..spine import driver as drv
from ..spine.contracts import BugGateResult, Candidate, Proposal, Verdict
from ..spine.keeper import KEPT

SETTINGS = {
    "track": "perf",
    "benchmarkCommand": "python bench.py",
    "benchmarkCanaryCommand": "python bench.py --slow",
    "benchmarkResultMode": "structured",
    "focusedTestPaths": ["tests/unit"],
    "fullRegressionTimeoutSeconds": 1234,
}


def request(body):
    req = make_mocked_request("PUT", "/config")
    req._read_bytes = json.dumps(body).encode()
    return req


@pytest.mark.asyncio
async def test_config_roundtrip_invalidates_calibration(monkeypatch):
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    store.write_json_atomic(store.config_path(), {"target_url": "https://github.com/o/a"})
    store.write_json_atomic(store.ruler_dir() / "ruler.json", {"status": "calibrated"})
    response = await routes._handle_put_config(request(SETTINGS))
    assert response.status == 200
    assert isinstance(response, web.Response)
    assert isinstance(response.body, bytes)
    assert json.loads(response.body)["rejected"] == []
    saved = store.read_json(store.config_path())
    assert all(saved[key] == value for key, value in SETTINGS.items())
    assert store.read_json(store.ruler_dir() / "ruler.json")["status"] == "uncalibrated"
    saved["target_url"] = "https://github.com/o/b"
    store.restore_test_environment(saved)
    assert saved["focusedTestPaths"] == []
    assert saved["benchmarkCommand"] == ""
    assert saved["fullRegressionTimeoutSeconds"] == 900
    saved["target_url"] = "https://github.com/o/a.git"
    store.restore_test_environment(saved)
    assert all(saved[key] == value for key, value in SETTINGS.items())


@pytest.mark.parametrize(
    "patch",
    [
        {"focusedTestPaths": ["../tests"]},
        {"focusedTestPaths": ["-k x"]},
        {"focusedTestPaths": ["/tests"]},
        {"focusedTestPaths": "tests"},
        {"focusedTestPaths": ["tests/*.py"]},
        {"fullRegressionTimeoutSeconds": 0},
        {"fullRegressionTimeoutSeconds": True},
        {"fullRegressionTimeoutSeconds": float("inf")},
        {"fullRegressionTimeoutSeconds": 10**400},
        {"benchmarkResultMode": "unknown"},
        {"track": "unknown"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_config_preserves_previous_value(patch, monkeypatch):
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    original = {"branch": "feature"}
    store.write_json_atomic(store.config_path(), original)
    assert (await routes._handle_put_config(request(patch))).status == 400
    assert store.read_json(store.config_path()) == original


def test_focused_iteration_and_unrestricted_final_regression(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gp, "_run", run)
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        focused_test_paths=["tests/unit"],
        full_regression_timeout_s=1234,
    )
    assert profile.build_gate.suite_scope == ["tests/unit"]
    assert profile.bug_runner.suite_scope == ["tests/unit"]
    assert profile.full_regression(timeout=1234) is True
    argv, kwargs = calls[-1]
    assert "tests/unit" not in argv
    assert argv[-1] == "."
    assert kwargs["timeout"] == 1234
    assert gp._SUITE_TIMEOUT_S == 900


def make_driver(tmp_path, monkeypatch, passed=True):
    d = object.__new__(drv.Driver)
    d.clone = tmp_path
    d.branch = "feature"
    d.log = logging.getLogger(__name__)
    d._stop = False
    d._repository_retired = False
    d._rollback_failed = False
    d._run_deadline = time.monotonic() + 5000
    d._regression_tree = ""
    d.cost_meter = lambda: 0
    d.caps = drv.BudgetCaps(max_cost_usd=50)
    d.profile = SimpleNamespace(
        final_regression_required=True,
        full_regression_timeout_s=900,
        full_regression=Mock(return_value=passed),
    )
    monkeypatch.setattr(d, "_retire_if_unsafe", Mock(return_value=False))
    monkeypatch.setattr(d, "_progress", Mock())
    monkeypatch.setattr(d, "_regression_tree_id", Mock(return_value="tree"))
    monkeypatch.setattr(drv, "_git", lambda *args: subprocess.CompletedProcess([], 0, "base", ""))
    return d


@pytest.mark.parametrize(
    "failure",
    [
        "red",
        "timeout",
        "stop_before",
        "stop_during",
        "budget_before",
        "budget_during",
        "changed_tree",
        "dirty_tree",
        "cost",
    ],
)
def test_final_regression_refuses_unfinished_stopped_or_changed(tmp_path, monkeypatch, failure):
    d = make_driver(tmp_path, monkeypatch)
    if failure == "red":
        d.profile.full_regression.return_value = False
    elif failure == "timeout":
        d.profile.full_regression.side_effect = subprocess.TimeoutExpired("pytest", 900)
    elif failure == "stop_before":
        d._stop = True
    elif failure == "stop_during":

        def run(**_):
            d._stop = True
            return True

        d.profile.full_regression.side_effect = run
    elif failure == "budget_before":
        d._run_deadline = time.monotonic() + 900
    elif failure == "budget_during":

        def run(**_):
            d._run_deadline = 0
            return True

        d.profile.full_regression.side_effect = run
    elif failure == "changed_tree":
        d._regression_tree_id.side_effect = ["before", "after"]
    elif failure == "dirty_tree":
        d._regression_tree_id.return_value = ""
    elif failure == "cost":
        d.cost_meter = lambda: 50
    assert d._run_final_regression() is False
    if failure in {"stop_before", "budget_before", "dirty_tree", "cost"}:
        d.profile.full_regression.assert_not_called()


def test_rebase_must_repeat_regression_on_new_tree(tmp_path, monkeypatch):
    d = make_driver(tmp_path, monkeypatch)
    assert d._run_final_regression()
    d._regression_tree_id.return_value = "rebased-tree"
    assert not d._final_regression_current()
    assert d._reverify_head()
    assert d._regression_tree == "rebased-tree"
    assert d.profile.full_regression.call_count == 2


@pytest.mark.parametrize("kind", ["bug", "perf"])
@pytest.mark.parametrize("passed", [True, False])
def test_full_regression_precedes_every_kept_record_and_emit(tmp_path, monkeypatch, kind, passed):
    d = make_driver(tmp_path, monkeypatch, passed)
    events = []

    def record(event, result):
        events.append(event)
        return result

    d.profile.full_regression.side_effect = lambda **kw: record("regression", passed)
    d.archive = Mock()
    d.archive.save_candidate.side_effect = lambda **kw: record("archive", "diff")
    d.archive.append_row.side_effect = lambda row: events.append(row["status"])
    d.stats = SimpleNamespace(kept=0, not_kept=0)
    d.measurer = SimpleNamespace(reps=1)
    d.ledger = Mock()
    d._reset_provisional = Mock()
    d._commit_winner_provisional = Mock(side_effect=lambda _: record("commit", True))
    d._commit_bug_winner_provisional = d._commit_winner_provisional
    d.pr_pipeline = Mock()
    outcome = SimpleNamespace(
        repository_retired=True, filed=False, committed_ready=False, status="failed_verify"
    )
    d.pr_pipeline.emit_perf.side_effect = lambda **kw: record("emit", outcome)
    d.pr_pipeline.emit_bug.side_effect = lambda **kw: record("emit", outcome)
    winner = Proposal(
        cand_id="candidate",
        candidate=Candidate(kind=kind, target="a.py"),
        diff="patch",
        description="fix",
        worktree=tmp_path,
        branch="candidate",
    )
    if kind == "bug":
        d._apply_bug_winner(1, winner, BugGateResult(passed=True, reason="green"))
    else:
        verdict = Verdict(keep=True, status=KEPT, winner=winner, reason="win")
        d._apply_verdict(1, "base", verdict, [(winner, KEPT, None)], 1, {})
    if passed:
        assert (
            events.index("commit")
            < events.index("regression")
            < events.index(KEPT)
            < events.index("emit")
        )
    else:
        assert events == ["commit", "regression"]
        d._reset_provisional.assert_called_once_with("base")
        assert d.stats.kept == 0


def test_manual_publication_requirement_survives_config_changes():
    fp = "abc"
    assert commit.manual_regression_required({"focusedTestPaths": ["tests"]}, fp)
    marker = store.pr_queue_dir() / f"{fp}.regression-required"
    marker.write_text("required", encoding="utf-8")
    assert commit.manual_regression_required({}, fp)


def test_legacy_profile_does_not_run_extra_suite(tmp_path, monkeypatch):
    d = make_driver(tmp_path, monkeypatch)
    d.profile.final_regression_required = False
    assert d._run_final_regression()
    d.profile.full_regression.assert_not_called()


@pytest.mark.parametrize("direct", [True, False])
def test_pipeline_refuses_queue_and_publication_without_current_regression(tmp_path, direct):
    from ..spine.pr_pipeline import CrPipeline

    pipeline = object.__new__(CrPipeline)
    pipeline.direct_commit = direct
    pipeline.ledger = Mock()
    recipe = SimpleNamespace(pr_queue_dir=tmp_path / "queue", draft=Mock())
    profile = SimpleNamespace(
        final_regression_required=True, final_regression_guard=lambda: False, pr_recipe=recipe
    )
    outcome = pipeline._draft_and_record(
        profile=profile,
        fp="fp",
        kind="bug",
        target="a.py",
        summary="fix",
        description="fixed",
        diff="diff",
        note="note",
    )
    assert not outcome.filed and not outcome.committed_ready
    recipe.draft.assert_not_called()
    assert not recipe.pr_queue_dir.exists()


@pytest.mark.parametrize("manual", ["draft", "commit"])
@pytest.mark.asyncio
async def test_manual_routes_schedule_marked_queue_before_materialization(monkeypatch, manual):
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    fp = "a" * 40
    queue = store.pr_queue_dir()
    (queue / f"{fp}.regression-required").write_text("required", encoding="utf-8")
    materialize = Mock(side_effect=AssertionError("must run on supervised worker"))
    monkeypatch.setattr(commit, "materialize_queued_diff", materialize)
    supervisor = Mock()
    supervisor.publish.return_value = {"publication": "pending", "status": "running"}
    monkeypatch.setattr(routes.runner, "get_supervisor", lambda: supervisor)
    req = make_mocked_request("POST", "/publish/" + fp, match_info={"fp": fp})
    handler = routes._handle_commit if manual == "commit" else routes._handle_draft_pr
    response = await handler(req)
    assert response.status == 202
    assert json.loads(response.body)["publication"] == "pending"
    supervisor.publish.assert_called_once()
    materialize.assert_not_called()


def test_calibration_rejects_settings_changed_on_another_branch(monkeypatch):
    monkeypatch.setattr(store, "measurement_provenance", lambda _: {"sourceRevision": "base"})
    from ..backend import progress

    config = {"target_url": "https://github.com/o/a", "branch": "feature", **SETTINGS}
    store.write_json_atomic(store.config_path(), config)
    store.write_json_atomic(
        store.ruler_dir() / "ruler.json",
        {
            "status": "calibrated",
            "measurementConfig": store.measurement_identity(config),
            "provenance": {"sourceRevision": "base"},
        },
    )
    assert progress.ruler_calibrated()
    config["fullRegressionTimeoutSeconds"] = 2345
    store.write_json_atomic(store.config_path(), config)
    assert not progress.ruler_calibrated()


@pytest.mark.parametrize("returncode", [1, 2, 3, 4, 5, -15])
def test_full_regression_never_waives_a_nonzero_exit(tmp_path, monkeypatch, returncode):
    monkeypatch.setattr(
        gp,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, returncode, "1 passed", ""),
    )
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path, pr_queue_dir=tmp_path / "queue", focused_test_paths=["tests"]
    )
    assert profile.full_regression(timeout=900) is False


def test_unexpected_regression_error_rolls_back_before_propagating(tmp_path, monkeypatch):
    d = make_driver(tmp_path, monkeypatch)
    d._reset_provisional = Mock()
    d.profile.full_regression.side_effect = TypeError("broken integration")
    with pytest.raises(TypeError, match="broken integration"):
        d._prepare_finalist(Mock(), lambda winner: True)
    d._reset_provisional.assert_called_once_with("base")


def test_budget_expiring_during_preflight_does_not_launch_regression(tmp_path, monkeypatch):
    d = make_driver(tmp_path, monkeypatch)

    def probe(_):
        d._run_deadline = time.monotonic() + 100
        return False

    d._retire_if_unsafe.side_effect = probe
    assert not d._run_final_regression()
    d.profile.full_regression.assert_not_called()


def test_recipe_refuses_a_stopped_finalist_before_queue_or_push(tmp_path, monkeypatch):
    from ..backend import clone_setup
    from ..profiles.github_repo.pr_recipe import GitHubPRRecipe

    recipe = GitHubPRRecipe(
        user="test",
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        base_ref="origin/feature",
    )
    recipe.publication_guard = lambda: False
    with pytest.raises(RuntimeError, match="full regression"):
        recipe.draft(summary="fix", description="fix", diff="diff", fingerprint="fp")
    assert not recipe.pr_queue_dir.exists()
    monkeypatch.setattr(clone_setup, "_repository_is_isolated", lambda _: True)
    monkeypatch.setattr(recipe, "_resolve_fetch_url", lambda: "https://example.invalid/o/r")
    monkeypatch.setattr(recipe, "_authorize", lambda _: (True, ""))
    monkeypatch.setattr(recipe, "_scan_pushable_content", lambda: (True, ""))
    git = Mock(side_effect=AssertionError("must not publish"))
    monkeypatch.setattr(recipe, "_git", git)
    passed, note = recipe._push_fix_branch(branch="auto-improvement/bug-fp")
    assert not passed and "full regression" in note
    git.assert_not_called()


def test_direct_push_refuses_a_tree_changed_after_regression(tmp_path, monkeypatch):
    d = make_driver(tmp_path, monkeypatch)
    assert d._run_final_regression()
    d._regression_tree_id.return_value = "changed"
    monkeypatch.setattr(drv, "require_pinned", lambda _: None)
    push = Mock(side_effect=AssertionError("must not publish"))
    monkeypatch.setattr(drv.subprocess, "run", push)
    result = d._push_with_rebase("https://example.invalid/o/r", "feature", "a.py", "commit")
    assert result.returncode != 0
    push.assert_not_called()


@pytest.mark.asyncio
async def test_unrelated_config_save_preserves_default_calibration(monkeypatch):
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    store.write_json_atomic(store.config_path(), {"target_url": "https://github.com/o/a"})
    store.write_json_atomic(store.ruler_dir() / "ruler.json", {"status": "calibrated"})
    response = await routes._handle_put_config(request({"directCommit": False}))
    assert response.status == 200
    assert store.read_json(store.ruler_dir() / "ruler.json")["status"] == "calibrated"


@pytest.mark.parametrize("key", ["PYTEST_ADDOPTS", "pytest_addopts"])
def test_focused_regression_refuses_environment_test_selection(tmp_path, key):
    with pytest.raises(ValueError, match="PYTEST_ADDOPTS"):
        gp.GitHubRepoProfile(
            clone_path=tmp_path,
            pr_queue_dir=tmp_path / "queue",
            focused_test_paths=["tests/unit"],
            test_environment={"kind": "gateway", "variables": {key: "-k small"}},
        )
