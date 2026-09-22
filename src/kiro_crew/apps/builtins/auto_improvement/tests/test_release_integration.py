from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from .. import profiles
from ..backend import clone_setup, commit, progress, routes, runner, store
from ..spine.contracts import BugGateResult, Candidate, Proposal, Verdict
from ..spine.keeper import KEPT
from .test_bounded_measurement import make_driver
from .test_environment import repository_runner  # noqa: F401


@pytest.mark.parametrize("track", ["bug", "perf"])
@pytest.mark.parametrize("failure", ["save_candidate", "append_row", "emit"])
def test_archive_and_publication_exception_roll_back(tmp_path, monkeypatch, track, failure):
    d = make_driver(tmp_path, monkeypatch)
    d.stats = SimpleNamespace(kept=0, not_kept=0)
    d.measurer = SimpleNamespace(reps=1)
    d.archive = Mock()
    d.ledger = Mock()
    d.pr_pipeline = Mock()
    d._commit_winner_provisional = Mock(return_value=True)
    d._commit_bug_winner_provisional = Mock(return_value=True)
    d._reset_provisional = Mock()
    target = (
        getattr(d.pr_pipeline, "emit_" + track)
        if failure == "emit"
        else getattr(d.archive, failure)
    )
    target.side_effect = OSError("archive storage unavailable")
    winner = Proposal(
        cand_id="candidate",
        candidate=Candidate(kind=track, target="a.py"),
        diff="patch",
        description="fix",
        worktree=tmp_path,
        branch="candidate",
    )
    with pytest.raises(OSError, match="storage unavailable"):
        if track == "bug":
            d._apply_bug_winner(1, winner, BugGateResult(passed=True, reason="green"))
        else:
            d._apply_verdict(
                1,
                "base",
                Verdict(keep=True, status=KEPT, winner=winner, reason="win"),
                [(winner, KEPT, None)],
                1,
                {},
            )
    d._reset_provisional.assert_called_once_with("base")


@pytest.mark.asyncio
async def test_http_and_mcp_reader_invalidate_changed_provenance(monkeypatch):
    config = {"target_url": "https://example.invalid/o/r"}
    store.write_json_atomic(store.config_path(), config)
    current = {"sourceRevision": "base", "execution": {"sha256": "one"}}
    monkeypatch.setattr(store, "measurement_provenance", lambda _: current)
    store.write_json_atomic(
        store.ruler_dir() / "ruler.json",
        {
            "status": "calibrated",
            "measurementConfig": store.measurement_identity(config),
            "provenance": dict(current),
        },
    )
    assert progress.ruler_calibrated()
    current["sourceRevision"] = "changed"
    response = await routes._handle_ruler(make_mocked_request("GET", "/ruler"))
    assert isinstance(response, web.Response)
    assert isinstance(response.body, bytes)
    assert json.loads(response.body)["status"] == "uncalibrated"
    assert not progress.ruler_calibrated()


@pytest.mark.parametrize(
    "field,value",
    [("noiseFloorSeconds", 4), ("bandCapMs", 100), ("benchmarkProtectedPaths", ["controls"])],
)
def test_measurement_identity_includes_controls(field, value):
    assert store.measurement_identity({field: value}) != store.measurement_identity({})


def test_unavailable_provenance_fails_closed(monkeypatch):
    store.write_json_atomic(store.ruler_dir() / "ruler.json", {"status": "calibrated"})
    monkeypatch.setattr(store, "measurement_provenance", Mock(side_effect=OSError("unavailable")))
    assert not progress.ruler_calibrated()


@pytest.mark.parametrize("failure", ["red", "timeout", "stop", "tree", "dirty", "isolation"])
def test_manual_regression_rejects_incomplete_or_changed_tree(monkeypatch, tmp_path, failure):
    state = {"stop": False, "head": "candidate", "dirty": "", "isolated": True}
    monkeypatch.setattr(commit, "_repository_is_isolated", lambda _: state["isolated"])
    monkeypatch.setattr(
        commit,
        "_git",
        lambda clone, *args, **kw: subprocess.CompletedProcess(
            args, 0, state["dirty"] if args[0] == "status" else state["head"], ""
        ),
    )

    def regression(**kwargs):
        assert kwargs["timeout"] == 90
        if failure == "timeout":
            raise subprocess.TimeoutExpired("pytest", 90)
        if failure == "stop":
            state["stop"] = True
        if failure == "tree":
            state["head"] = "changed"
        if failure == "dirty":
            state["dirty"] = " M changed.py"
        if failure == "isolation":
            state["isolated"] = False
        return failure != "red"

    profile = SimpleNamespace(
        full_regression_timeout_s=90,
        require_environment=Mock(),
        ruler=SimpleNamespace(),
        full_regression=regression,
    )
    monkeypatch.setattr(profiles, "build_profile", lambda _: profile)
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        commit.run_publication_regression({"clone": str(tmp_path)}, lambda: state["stop"])


def test_manual_regression_guard_checks_again_at_publication(monkeypatch, tmp_path):
    head = ["candidate"]
    monkeypatch.setattr(commit, "_repository_is_isolated", lambda _: True)
    monkeypatch.setattr(
        commit,
        "_git",
        lambda clone, *args, **kw: subprocess.CompletedProcess(
            args, 0, "" if args[0] == "status" else head[0], ""
        ),
    )
    profile = SimpleNamespace(
        full_regression_timeout_s=90,
        require_environment=Mock(),
        ruler=SimpleNamespace(),
        full_regression=Mock(return_value=True),
    )
    monkeypatch.setattr(profiles, "build_profile", lambda _: profile)
    guard = commit.run_publication_regression({"clone": str(tmp_path)}, lambda: False)
    assert guard()
    head[0] = "other"
    assert not guard()


@pytest.mark.parametrize("outcome", ["raise", "queued", "published"])
def test_supervised_publication_reports_terminal_result_and_rolls_back(
    monkeypatch, tmp_path, outcome
):
    store.write_json_atomic(store.config_path(), {"clone": str(tmp_path)})
    monkeypatch.setattr(clone_setup, "_repository_is_isolated", lambda _: True)
    monkeypatch.setattr(commit, "_git", lambda *a: subprocess.CompletedProcess(a, 0, "base", ""))
    rollback = Mock()
    monkeypatch.setattr(commit, "safe_rollback", rollback)
    queue = store.pr_queue_dir() / "candidate.diff"
    queue.write_text("patch")

    def operation(regression):
        assert callable(regression)
        if outcome == "raise":
            raise OSError("disk full")
        return {
            "ok": outcome == "published",
            "pr": "https://example.invalid/pr/1",
            "detail": "still queued",
        }

    supervisor = runner.RunSupervisor()
    supervisor._publish_loop(operation)
    state = supervisor.status()
    assert state["status"] == ("done" if outcome == "published" else "error")
    assert rollback.call_count == (0 if outcome == "published" else 1)
    assert queue.read_text() == "patch"
    if outcome == "published":
        assert state["stats"]["publication"] == "published"


def test_unsafe_rollback_quarantines_without_git(monkeypatch, tmp_path):
    monkeypatch.setattr(commit, "_repository_is_isolated", lambda _: False)
    git = Mock()
    mark, retire = Mock(), Mock()
    monkeypatch.setattr(commit, "_git", git)
    monkeypatch.setattr(clone_setup, "_mark_clone_quarantined", mark)
    monkeypatch.setattr(clone_setup, "_retire_unsafe_clone", retire)
    with pytest.raises(RuntimeError, match="isolation"):
        commit.safe_rollback(tmp_path, "base")
    git.assert_not_called()
    mark.assert_called_once()
    retire.assert_called_once_with(tmp_path)


def test_interpreter_content_change_invalidates_provenance_without_timestamp_change(
    monkeypatch, tmp_path
):
    import os

    executable = tmp_path / "python"
    executable.touch(mode=0o700)
    executable.chmod(0o700)
    executable.write_bytes(b"first interpreter")
    config = {
        "clone": str(tmp_path),
        "testEnvironment": {"kind": "python", "pythonExecutable": str(executable)},
    }
    monkeypatch.setattr(clone_setup, "_repository_is_isolated", lambda _: True)
    monkeypatch.setattr(
        commit,
        "_git",
        lambda clone, *args: subprocess.CompletedProcess(
            args, 0, "" if args[0] == "status" else "base", ""
        ),
    )
    original = store.measurement_provenance(config)
    timestamp = executable.stat().st_mtime_ns
    executable.write_bytes(b"other interpreter")
    os.utime(executable, ns=(timestamp, timestamp))
    assert store.measurement_provenance(config) != original


def test_publication_uses_authenticated_capture_and_reserves_worker(monkeypatch):
    thread = Mock()
    thread.is_alive.return_value = False
    monkeypatch.setattr(runner.threading, "Thread", Mock(return_value=thread))
    capture = Mock(side_effect=lambda callback: callback)
    monkeypatch.setattr(runner, "capture_app_execution", capture)
    supervisor = runner.RunSupervisor()
    receipt = supervisor.publish(Mock())
    assert receipt["publication"] == "pending"
    assert supervisor.status()["status"] == "running"
    capture.assert_called_once_with(supervisor._publish_loop)
    thread.start.assert_called_once()
    with pytest.raises(RuntimeError, match="already active"):
        supervisor.publish(Mock())


def test_marked_finalist_cannot_restore_pytest_selection_by_clearing_focus(tmp_path):
    with pytest.raises(ValueError, match="PYTEST_ADDOPTS"):
        commit.run_publication_regression(
            {
                "clone": str(tmp_path),
                "focusedTestPaths": [],
                "testEnvironment": {
                    "kind": "gateway",
                    "variables": {"PYTEST_ADDOPTS": "-k subset"},
                },
            },
            lambda: False,
        )


def test_published_result_survives_cleanup_failure(monkeypatch, tmp_path):
    store.write_json_atomic(store.config_path(), {"clone": str(tmp_path)})
    monkeypatch.setattr(clone_setup, "_repository_is_isolated", lambda _: True)
    monkeypatch.setattr(commit, "_git", lambda *a: subprocess.CompletedProcess(a, 0, "base", ""))
    rollback = Mock()
    monkeypatch.setattr(commit, "safe_rollback", rollback)
    supervisor = runner.RunSupervisor()
    supervisor._publish_loop(
        lambda regression: {
            "ok": True,
            "pr": "https://example.invalid/pr/1",
            "cleanupError": "clone quarantined",
        }
    )
    assert supervisor.status()["status"] == "error"
    assert supervisor.status()["stats"]["publication"] == "published"
    rollback.assert_not_called()


def publication_driver(tmp_path, monkeypatch, track, *, focused, draft=False):
    from ..spine.pr_pipeline import CrOutcome

    d = make_driver(tmp_path, monkeypatch)
    d.profile.final_regression_required = focused
    d.profile.isolation = SimpleNamespace(base_ref="base")
    d.stats = SimpleNamespace(kept=0, not_kept=0, filed=0)
    d.measurer = SimpleNamespace(reps=1)
    d.archive = Mock()
    d.ledger = Mock()
    d.pr_pipeline = Mock()
    d.pushed_sha = "stale-previous-winner"
    d._commit_winner_provisional = Mock(return_value=True)
    d._commit_bug_winner_provisional = Mock(return_value=True)
    d._finalize_winner_commit = Mock(return_value="candidate-sha")
    d._finalize_bug_winner_commit = Mock(return_value="candidate-sha")
    d._reset_provisional = Mock()
    d._direct_push = Mock(return_value=True)
    outcome = CrOutcome(
        fp="fp",
        status="filed" if draft else "seen",
        filed=draft,
        cr="https://example.invalid/pr/1" if draft else "",
        committed_ready=not draft,
    )
    getattr(d.pr_pipeline, "emit_" + track).return_value = outcome
    winner = Proposal(
        cand_id="candidate",
        candidate=Candidate(kind=track, target="a.py"),
        diff="patch",
        description="fix",
        worktree=tmp_path,
        branch="candidate",
    )

    def apply():
        if track == "bug":
            return d._apply_bug_winner(1, winner, BugGateResult(passed=True, reason="green"))
        return d._apply_verdict(
            1,
            "base",
            Verdict(keep=True, status=KEPT, winner=winner, reason="win"),
            [(winner, KEPT, None)],
            1,
            {},
        )

    return d, apply, outcome


@pytest.mark.parametrize("track", ["bug", "perf"])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("rebased", [False, True])
def test_landed_push_ledger_failure_preserves_publication(
    tmp_path, monkeypatch, track, focused, rebased, caplog
):
    d, apply, _ = publication_driver(tmp_path, monkeypatch, track, focused=focused)

    def push(**kwargs):
        assert d.pushed_sha == ""
        if rebased:
            d.pushed_sha = "rebased-sha"
        d.ledger.record.side_effect = OSError("ledger disk full")
        return True

    d._direct_push.side_effect = push
    with pytest.raises(OSError, match="ledger disk full"):
        apply()
    landed = "rebased-sha" if rebased else "candidate-sha"
    assert d._publication_failure == {"landed_sha": landed}
    assert d.ledger.record.call_args.args[0].cr == landed
    assert d._progress.call_args.kwargs["landed_sha"] == landed
    assert "do not retry publication" in caplog.text
    assert d._stop
    d._reset_provisional.assert_not_called()
    apply()
    d._direct_push.assert_called_once()


@pytest.mark.parametrize("track", ["bug", "perf"])
@pytest.mark.parametrize("failure", ["ledger", "finalize"])
def test_draft_publication_preserved_on_bookkeeping_failure(tmp_path, monkeypatch, track, failure):
    from ..spine.pr_pipeline import CrPipeline

    d, apply, outcome = publication_driver(tmp_path, monkeypatch, track, focused=True, draft=True)
    if failure == "ledger":
        pipeline = object.__new__(CrPipeline)
        pipeline.ledger, pipeline.log = d.ledger, d.log
        pipeline.direct_commit = False
        recipe = SimpleNamespace(draft=Mock(return_value=outcome.cr))
        profile = SimpleNamespace(pr_recipe=recipe, final_regression_required=False)

        def published(**kwargs):
            d.ledger.record.side_effect = OSError("ledger disk full")
            return outcome.cr

        recipe.draft.side_effect = published
        getattr(d.pr_pipeline, "emit_" + track).side_effect = (
            lambda **kwargs: pipeline._draft_and_record(
                profile=profile,
                fp="fp",
                kind=track,
                target="a.py",
                summary="fix",
                description="fix",
                diff="patch",
                note="published",
            )
        )
    else:
        method = "_finalize_bug_winner_commit" if track == "bug" else "_finalize_winner_commit"
        getattr(d, method).side_effect = OSError("amend failed")
    with pytest.raises((OSError, RuntimeError), match="disk full|amend failed"):
        apply()
    assert d._publication_failure == {"cr": outcome.cr}
    d._reset_provisional.assert_not_called()
    apply()
    getattr(d.pr_pipeline, "emit_" + track).assert_called_once()
    if failure == "ledger":
        recipe.draft.assert_called_once()


@pytest.mark.parametrize("track", ["bug", "perf"])
@pytest.mark.parametrize("draft", [False, True])
def test_previous_publication_does_not_own_next_winner(tmp_path, monkeypatch, track, draft):
    d, apply, _ = publication_driver(tmp_path, monkeypatch, track, focused=False, draft=draft)
    apply()
    d._reset_provisional.reset_mock()
    d.archive.save_candidate.side_effect = OSError("archive disk full")
    with pytest.raises(OSError, match="archive disk full"):
        apply()
    d._reset_provisional.assert_called_once_with("base")
    assert not getattr(d, "_publication_failure", None)
    assert d._direct_push.call_count == (0 if draft else 1)


@pytest.fixture
def authenticated_repository_runner(request):
    return request.getfixturevalue("repository_runner")


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", ["draft", "commit"])
async def test_authenticated_http_publication_admits_runner(
    monkeypatch, authenticated_repository_runner, manual
):
    import asyncio
    import threading

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import revocation_gen, token_auth, token_secret
    from kiro_crew.platform.app_execution import current_app_execution

    state = authenticated_repository_runner
    monkeypatch.setattr(token_secret, "_SECRET", b"publication-test-signing-key")
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(token_auth, "_sel_fn", lambda: state.audit)
    supervisor = runner.RunSupervisor()
    monkeypatch.setattr(runner, "get_supervisor", lambda: supervisor)
    monkeypatch.setattr(clone_setup, "_repository_is_isolated", lambda _: True)
    monkeypatch.setattr(commit, "_repository_is_isolated", lambda _: True)
    monkeypatch.setattr(clone_setup, "resolve_origin_url", lambda _: "")
    monkeypatch.setattr(
        commit,
        "_git",
        lambda clone, *args, **kw: subprocess.CompletedProcess(
            args, 0, "" if args[0] == "status" else "candidate", ""
        ),
    )
    monkeypatch.setattr(commit, "safe_rollback", Mock())
    monkeypatch.setattr(
        commit, "materialize_queued_diff", lambda **kw: {"ok": True, "base": "base"}
    )
    monkeypatch.setattr(commit, "commit_staged_for_draft", lambda **kw: {"ok": True})
    monkeypatch.setattr(routes, "ledger_admin_record", Mock())
    observed = []

    def regression(**kwargs):
        observed.append((current_app_execution(), threading.current_thread().name))
        return (
            state.adapter.run(
                state.adapter.python_argv("-c", "pass"), cwd=state.clone, timeout=5
            ).returncode
            == 0
        )

    profile = SimpleNamespace(
        full_regression_timeout_s=5, ruler=SimpleNamespace(), full_regression=regression
    )
    monkeypatch.setattr(profiles, "build_profile", lambda _: profile)

    def commit_finding(fp, *, regression):
        assert regression(store.read_json(store.config_path()))()
        return {"ok": True, "fp": fp}

    monkeypatch.setattr(commit, "commit_finding", commit_finding)
    recipe = SimpleNamespace(publication_guard=None)

    def draft(**kwargs):
        assert recipe.publication_guard()
        return "https://example.invalid/pr/1"

    recipe.draft = draft
    monkeypatch.setattr(routes, "GitHubPRRecipe", lambda **kw: recipe)
    fp = "a" * 40
    queue = store.pr_queue_dir()
    for suffix, content in (
        ("regression-required", "required"),
        ("diff", "patch"),
        ("pr.md", "# Fix"),
    ):
        (queue / f"{fp}.{suffix}").write_text(content, encoding="utf-8")
    path = f"/api/apps/auto-improvement/{manual}/{{fp}}"
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app.router.add_post(
        path, routes._handle_draft_pr if manual == "draft" else routes._handle_commit
    )
    token = token_auth.generate_token("publication-tester")
    token_auth.bind_token_ip(token, "127.0.0.1")
    token_auth.mark_consumed(token)
    try:
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            url = path.replace("{fp}", fp)
            rejected = await client.post(url)
            assert rejected.status in (401, 403)
            assert supervisor._thread is None
            port = client.server.port
            response = await client.post(
                url,
                headers={
                    "Cookie": f"mc_token_{port}={token}",
                    "Origin": f"http://127.0.0.1:{port}",
                },
            )
            assert response.status == 202, await response.text()
            thread = supervisor._thread
            assert thread is not None
            await asyncio.to_thread(thread.join, 10)
            assert not thread.is_alive()
            assert supervisor.status()["status"] == "done", supervisor.status()
    finally:
        if supervisor._thread is not None:
            await asyncio.to_thread(supervisor._thread.join, 10)
        token_auth._state.clear_all()
    assert len(observed) == 1
    execution, thread_name = observed[0]
    assert execution is not None
    assert execution.user == "publication-tester" and execution.app == store.APP_NAME
    assert thread_name.startswith("auto-improvement-publish-")
    assert len(state.calls) == 1
    assert state.audit.log_governance_decision.call_count == 5
    assert current_app_execution() is None
