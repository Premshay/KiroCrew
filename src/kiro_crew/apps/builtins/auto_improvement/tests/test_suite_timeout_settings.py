"""Routine deadlines reach the executor without changing the finalist deadline."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ..profiles.github_repo import profile as gp


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, "900", None, float("nan"), float("inf"), 86401]
)
def test_invalid_suite_timeout_refused(value):
    with pytest.raises(ValueError, match="suiteTimeoutSeconds"):
        gp.normalize_measurement_config({"suiteTimeoutSeconds": value})


def test_default_and_independent_regression_timeout(tmp_path):
    defaults = gp.normalize_measurement_config({})
    assert defaults["suiteTimeoutSeconds"] == 900
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        suite_timeout_s=2400,
        full_regression_timeout_s=3600,
    )
    assert profile.full_regression_timeout_s == 3600
    assert profile.ruler.measurement_constants["suiteTimeoutSeconds"] == "2400.0"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    profile.environment.run = run
    assert profile.full_regression(timeout=3500)
    assert run.call_args.kwargs["timeout"] == 3500


@pytest.mark.parametrize("has_scoped_tests", [True, False])
def test_allowlist_derived_suite_requires_final_regression(tmp_path, has_scoped_tests):
    (tmp_path / "package").mkdir()
    if has_scoped_tests:
        (tmp_path / "package" / "tests").mkdir()
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        allowed_globs=["package/**"],
    )
    assert profile.focused_test_paths == []
    assert profile.final_regression_required is has_scoped_tests
    assert profile.build_gate.suite_scope == (["package/tests"] if has_scoped_tests else [])


def test_allowlist_derived_scope_rejects_environment_test_filter(tmp_path):
    (tmp_path / "package" / "tests").mkdir(parents=True)
    with pytest.raises(ValueError, match="PYTEST_ADDOPTS"):
        gp.GitHubRepoProfile(
            clone_path=tmp_path,
            pr_queue_dir=tmp_path / "queue",
            allowed_globs=["package/**"],
            test_environment={"kind": "gateway", "variables": {"PYTEST_ADDOPTS": "-k small"}},
        )


@pytest.mark.parametrize(
    "operation", ["build", "suite", "red_green", "collect", "sample", "benchmark"]
)
def test_routine_deadline_reaches_executor(tmp_path, monkeypatch, operation):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "2 tests collected", ""))
    monkeypatch.setattr(gp, "_run", run)
    monkeypatch.setattr(gp, "_xdist_argv", lambda *args: ())
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        suite_timeout_s=2400,
        benchmark_cmd="python -c pass" if operation == "benchmark" else "",
    )
    if operation == "build":
        assert profile.build_gate.build_and_test(worktree=tmp_path, src=tmp_path).passed
    elif operation == "suite":
        assert profile.bug_runner.run_suite(src=tmp_path)[0]
    elif operation == "red_green":
        assert profile.bug_runner.run_reproducing_test(
            src=tmp_path, test_id="test_example.py::test_value", test_only=True
        )
    elif operation == "collect":
        assert profile.bug_runner.test_collects(src=tmp_path, test_path="test_example.py")
    else:
        assert profile.ruler._time_once(tmp_path)[1]
    assert run.called
    assert all(call.kwargs["timeout"] == 2400 for call in run.call_args_list)


@pytest.mark.parametrize("fails", [False, True])
def test_check_reports_actual_step_and_total_durations(tmp_path, monkeypatch, fails):
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path, pr_queue_dir=tmp_path / "queue", suite_timeout_s=2400
    )
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(gp.time, "perf_counter", lambda: clock.value)
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["timeout"])
        clock.value += 2 if len(calls) == 1 else 7
        if fails and len(calls) == 2:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "2 tests collected", "")

    profile.environment.run = run
    result = profile.check_environment()
    assert result["ok"] is not fails
    assert result["timings"] == {"interpreter": 2, "collection": 7}
    assert result["duration_seconds"] == 9
    assert "suite" not in result["timings"]
    assert calls == [gp._QUICK_TIMEOUT_S, 2400]


def test_benchmark_check_reports_workload_duration(tmp_path, monkeypatch):
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        benchmark_cmd="python -c pass",
        suite_timeout_s=2400,
    )
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(gp.time, "perf_counter", lambda: clock.value)

    def run(argv, **kwargs):
        clock.value += 4
        return subprocess.CompletedProcess(argv, 0, "", "")

    profile.environment.run = run
    result = profile.check_environment()
    assert result["ok"]
    assert result["timings"] == {"interpreter": 4, "benchmark": 4}
    assert result["duration_seconds"] == 8
    assert "tests_collected" not in result
