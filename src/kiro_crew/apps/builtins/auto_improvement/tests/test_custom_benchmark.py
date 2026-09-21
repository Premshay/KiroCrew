import json
import subprocess

import pytest

from ..profiles.github_repo import profile as gp
from ..profiles.github_repo.benchmark_result import PREFIX, parse_benchmark_result
from ..profiles.github_repo.environment import RunnerAdmissionError, TestEnvironment


def output(value=2, **changes):
    data = dict(
        schema_version=1, metric="duration", unit="seconds", value=value, workload_id="fixed"
    )
    data.update(changes)
    return PREFIX + " " + json.dumps(data)


def completed(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "ordinary output",
        PREFIX + " {",
        output() + "\n" + output(),
        " " + output(),
        output() + " extra",
        PREFIX + " []",
        output(schema_version=True),
        output(schema_version=2),
        output(metric=" "),
        output(workload_id=""),
        output(unit="ms"),
        output(value=True),
        output(value="2"),
        output(value=0),
        output(value=-1),
        output(value=float("nan")),
        output(value=float("inf")),
        output(extra=1),
        output().replace('"value": 2', '"value": 2, "value": 3'),
    ],
)
def test_rejects_invalid_structured_output(stdout):
    with pytest.raises(ValueError):
        parse_benchmark_result(completed(stdout), 9, "structured")


def test_inner_duration_and_outer_duration_are_distinct():
    result = parse_benchmark_result(completed("log\n" + output(2) + "\nlog"), 9, "structured")
    assert result.value == 2
    assert result.outer_seconds == 9
    assert result.identity == ("duration", "seconds", "fixed")


@pytest.mark.parametrize("mode", ["wall", "structured"])
def test_nonzero_exit_never_produces_sample(mode):
    with pytest.raises(ValueError):
        parse_benchmark_result(completed(output(), 1), 9, mode)


def ruler(monkeypatch, tmp_path, results, **options):
    samples = iter(results)
    calls = []

    def execute(argv, **kw):
        calls.append((argv, kw))
        assert "--collect-only" not in argv
        sample = next(samples)
        if isinstance(sample, Exception):
            raise sample
        return sample

    ticks = iter(range(1000))
    monkeypatch.setattr(gp.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(gp, "_collected_count", lambda *a, **kw: pytest.fail("custom collection"))
    env = TestEnvironment(
        {"kind": "gateway", "variables": {"WORKLOAD": "fixed"}}, tmp_path, execute
    )
    obj = gp.SuiteRuler(
        benchmark_cmd="python bench.py",
        benchmark_result_mode="structured",
        environment=env,
        **options,
    )
    return obj, calls


def test_measure_uses_inner_value_and_selected_environment(monkeypatch, tmp_path):
    obj, calls = ruler(monkeypatch, tmp_path, [completed(output(4)), completed(output(2))])
    result = obj.measure(base_src=tmp_path, cand_src=tmp_path, commit_sha="abc", scenario="custom")
    assert result.ok and result.primary_delta == -2
    assert result.secondary == {"base_outer_seconds": 1, "cand_outer_seconds": 1}
    assert result.stages.stages == {"benchmark": -2}
    assert len(calls) == 2
    assert all(argv[0] == obj.environment.python_argv()[0] for argv, _ in calls)
    assert all(kw["env"]["WORKLOAD"] == "fixed" for _, kw in calls)


@pytest.mark.parametrize(
    "bad",
    [
        completed(output(), 2),
        completed("bad"),
        completed(output(metric="other")),
        completed(output(workload_id="other")),
        subprocess.TimeoutExpired("benchmark", 1),
    ],
)
def test_baseline_fails_without_discarding_invalid_reps(monkeypatch, tmp_path, bad):
    obj, _ = ruler(monkeypatch, tmp_path, [completed(output()), bad, completed(output())])
    with pytest.raises(ValueError, match="baseline"):
        obj.baseline_samples(base_src=tmp_path, reps=3)


@pytest.mark.parametrize(
    "bad",
    [
        completed(output(), 1),
        completed("bad"),
        completed(output(metric="other")),
        completed(output(workload_id="other")),
    ],
)
def test_candidate_failure_or_identity_change_is_rejected(monkeypatch, tmp_path, bad):
    obj, _ = ruler(monkeypatch, tmp_path, [completed(output()), bad])
    assert not obj.measure(base_src=tmp_path, cand_src=tmp_path, commit_sha="", scenario="").ok


def test_custom_canary_is_sensitivity_only_on_baseline(monkeypatch, tmp_path):
    obj, calls = ruler(
        monkeypatch,
        tmp_path,
        [completed(output(2)), completed(output(5))] * gp._CANARY_REPS,
        benchmark_canary_cmd="pytest tests/slow.py",
    )
    result = obj.measure_canary(base_src=tmp_path)
    assert result.ok and result.primary_delta == -3
    assert "sensitivity only" in result.note
    assert all(kw["cwd"] == tmp_path for _, kw in calls)
    assert calls[1][0][1:] == ["-m", "pytest", "tests/slow.py"]


@pytest.mark.parametrize(
    "bad",
    [
        completed(output(), 1),
        completed("bad"),
        completed(output(workload_id="other")),
        completed(output(1)),
    ],
)
def test_invalid_or_unslowed_control_cannot_certify(monkeypatch, tmp_path, bad):
    obj, _ = ruler(
        monkeypatch,
        tmp_path,
        [completed(output(2)), bad] * gp._CANARY_REPS,
        benchmark_canary_cmd="python slow.py",
    )
    assert not obj.measure_canary(base_src=tmp_path).ok


def test_missing_control_fails_without_execution(monkeypatch, tmp_path):
    obj, calls = ruler(monkeypatch, tmp_path, [])
    assert not obj.measure_canary(base_src=tmp_path).ok
    assert not calls


def test_control_executable_restriction_and_admission(monkeypatch, tmp_path):
    obj, calls = ruler(
        monkeypatch, tmp_path, [completed(output())], benchmark_canary_cmd="sh slow.sh"
    )
    assert not obj.measure_canary(base_src=tmp_path).ok
    assert len(calls) == 1
    obj, _ = ruler(monkeypatch, tmp_path, [RunnerAdmissionError("denied")])
    with pytest.raises(RunnerAdmissionError):
        obj.measure(base_src=tmp_path, cand_src=tmp_path, commit_sha="", scenario="")


def test_factory_readiness_and_protected_harness(monkeypatch, tmp_path):
    profile = gp.build_profile(
        dict(
            clone=str(tmp_path),
            benchmarkCommand="python bench.py",
            benchmarkCanaryCommand="python -m controls.slow",
            benchmarkResultMode="structured",
            track="perf",
        )
    )
    calls = []

    def execute(argv, **kw):
        calls.append(argv)
        assert "--collect-only" not in argv
        return completed(output())

    monkeypatch.setattr(profile.environment, "_sandbox_run", execute)
    assert profile.check_environment()["ok"]
    assert len(calls) == 3
    assert profile.ruler.benchmark_result_mode == "structured"
    assert profile.calibration.canary_id == "custom_slowed_control_sensitivity"
    assert profile.build_gate.environment is profile.ruler.environment
    for path in [
        "bench.py",
        "controls/slow.py",
        "src/controls/slow.py",
        "controls/slow/__main__.py",
        "tests/test_existing.py",
        "pyproject.toml",
    ]:
        assert not profile.edit_allowlist.allows_changes([("M", path)])[0]
    assert profile.edit_allowlist.allows(["src/core.py"])[0]


@pytest.mark.parametrize(
    "options",
    [
        {"benchmark_result_mode": "bad"},
        {"benchmark_result_mode": "structured"},
        {"benchmark_canary_cmd": "python slow.py"},
    ],
)
def test_invalid_configuration_fails(options):
    with pytest.raises(ValueError):
        gp.SuiteRuler(**options)


def test_wall_mode_remains_default():
    assert gp.SuiteRuler(benchmark_cmd="python bench.py").benchmark_result_mode == "wall"
    assert parse_benchmark_result(completed("no marker"), 3, "wall").value == 3
    assert gp.SuiteRuler().primary_name == "suite_wall_seconds"


def test_oversized_integer_is_invalid():
    with pytest.raises(ValueError):
        parse_benchmark_result(completed(output(10**400)), 9, "structured")


@pytest.mark.parametrize("operation", ["measure", "canary"])
def test_failed_base_cannot_succeed(monkeypatch, tmp_path, operation):
    obj, calls = ruler(
        monkeypatch, tmp_path, [completed(output(), 1)], benchmark_canary_cmd="python slow.py"
    )
    result = (
        obj.measure_canary(base_src=tmp_path)
        if operation == "canary"
        else obj.measure(base_src=tmp_path, cand_src=tmp_path, commit_sha="", scenario="")
    )
    assert not result.ok
    assert len(calls) == 1


def test_custom_readiness_rejects_invalid_workload(monkeypatch, tmp_path):
    obj = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        benchmark_cmd="python bench.py",
        benchmark_result_mode="structured",
    )
    monkeypatch.setattr(obj.environment, "_sandbox_run", lambda *a, **kw: completed("bad"))
    assert not obj.check_environment()["ok"]


def test_protected_control_cannot_use_bug_test_addition_exception(tmp_path):
    obj = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        benchmark_cmd="python bench.py",
        benchmark_canary_cmd="pytest tests/test_bug_control.py",
    )
    assert not obj.edit_allowlist.allows_changes([("A", "tests/test_bug_control.py")])[0]


def test_suite_measurement_keeps_collection_stages(monkeypatch, tmp_path):
    obj = gp.SuiteRuler()
    samples = iter([(4, True), (1, True), (3, True), (1, True)])
    calls = []

    def time_once(tree, *, collect_only=False):
        calls.append(collect_only)
        return next(samples)

    monkeypatch.setattr(obj, "_time_once", time_once)
    monkeypatch.setattr(gp, "_collected_count", lambda *a, **kw: 4)
    result = obj.measure(base_src=tmp_path, cand_src=tmp_path, commit_sha="abc", scenario="suite")
    assert result.ok and result.primary_delta == -1
    assert result.stages.stages == {gp.STAGE_SUITE: -1, gp.STAGE_COLLECT: 0}
    assert calls == [False, True, False, True]


@pytest.mark.parametrize("factory", [False, True])
def test_configured_harness_roots_protect_imports_and_bug_additions(tmp_path, factory):
    roots = ["controls", "src/bench_helpers", "inputs/workload.json"]
    if factory:
        obj = gp.build_profile(
            dict(
                clone=str(tmp_path),
                benchmarkCommand="python bench.py",
                benchmarkProtectedPaths=roots,
            )
        )
    else:
        obj = gp.GitHubRepoProfile(
            clone_path=tmp_path,
            pr_queue_dir=tmp_path / "queue",
            benchmark_cmd="python bench.py",
            benchmark_protected_paths=roots,
        )
    assert obj.benchmark_protected_paths == roots
    for path in [
        "controls/__init__.py",
        "controls/timing.py",
        "src/bench_helpers/imported.py",
        "controls/test_bug_fake.py",
        "inputs/workload.json",
        "bench.py",
    ]:
        for status in ["A", "M", "D"]:
            assert not obj.edit_allowlist.allows_changes([(status, path)])[0]
    for path in ["src/production.py", "src/controls_extra.py", "other/timing.py"]:
        assert obj.edit_allowlist.allows_changes([("M", path)])[0]
    assert obj.edit_allowlist.allows_changes([("A", "tests/test_bug_real.py")])[0]


@pytest.mark.parametrize(
    "paths",
    [
        None,
        "controls",
        [None],
        [1],
        [""],
        [" "],
        ["."],
        [".."],
        ["a/../b"],
        ["/outside"],
        ["C:/outside"],
        ["a\\b"],
        [".git"],
        ["a/.GIT/config"],
        ["a/*"],
        ["a//b"],
        ["a/"],
        ["a\nb"],
        ["-x"],
    ],
)
def test_invalid_protected_path_configuration(paths):
    with pytest.raises(ValueError, match="benchmarkProtectedPaths"):
        gp.normalize_measurement_config({"benchmarkProtectedPaths": paths})


def test_protected_paths_reject_resolved_escape(monkeypatch, tmp_path):
    from pathlib import Path

    resolve = Path.resolve

    def redirected(path, *args, **kwargs):
        if path == tmp_path / "controls":
            return tmp_path.parent / "outside"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="inside the repository"):
        gp.GitHubRepoProfile(
            clone_path=tmp_path,
            pr_queue_dir=tmp_path / "queue",
            benchmark_protected_paths=["controls"],
        )


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("stop_after", [0, 1, 2, 5, 6])
def test_canary_stop_prevents_next_arm_and_certification(monkeypatch, tmp_path, custom, stop_after):
    if custom:
        obj, calls = ruler(
            monkeypatch,
            tmp_path,
            [completed(output(2)), completed(output(5))] * gp._CANARY_REPS,
            benchmark_canary_cmd="python slow.py",
        )
    else:
        obj, calls = gp.SuiteRuler(), []

        def time_once(tree, *, collect_only=False):
            calls.append(collect_only)
            return (1 if collect_only else 5), True

        monkeypatch.setattr(obj, "_time_once", time_once)
    obj.stop_check = lambda: len(calls) >= stop_after
    result = obj.measure_canary(base_src=tmp_path)
    assert not result.ok
    assert "Stop" in result.note
    assert len(calls) == stop_after
