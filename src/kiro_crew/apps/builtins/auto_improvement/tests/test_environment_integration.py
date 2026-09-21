from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import make_mocked_request

from ..backend import routes, runner, store
from ..profiles.github_repo import profile as gp
from ..profiles.github_repo.environment import RunnerAdmissionError
from ..spine.bug_gate import BugGate
from ..spine.contracts import BugReproducingTest, Candidate


def make_profile(tmp_path, monkeypatch, replies, selection=None):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return subprocess.CompletedProcess(argv, *reply)

    monkeypatch.setattr(gp, "_run", run)
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        test_environment=selection,
    )
    return profile, calls


@pytest.mark.parametrize(
    "reply,stage",
    [
        ((1, "", "No module named pytest"), "pytest"),
        ((2, "3 tests collected\n", "ImportError: missing dependency"), "collection"),
        ((0, "0 tests collected\n", ""), "collection"),
    ],
)
def test_readiness_refuses_incomplete_collection(tmp_path, monkeypatch, reply, stage):
    replies = [(0, "python", "")]
    if stage == "collection":
        replies.append((0, "pytest", ""))
    replies.append(reply)
    profile, calls = make_profile(tmp_path, monkeypatch, replies)
    result = profile.check_environment()
    assert result["ok"] is False
    assert result["diagnostic"]["stage"] == stage
    assert result["diagnostic"]["returncode"] == reply[0]
    assert all(call[1]["cwd"] == tmp_path for call in calls)


def test_selected_python_is_shared_and_collection_is_required(tmp_path, monkeypatch):
    profile, calls = make_profile(
        tmp_path,
        monkeypatch,
        [(0, "", ""), (0, "", ""), (0, "2 tests collected", "")],
        {"kind": "python", "pythonExecutable": "/opt/test/bin/python"},
    )
    assert profile.check_environment()["tests_collected"] == 2
    assert all(argv[0] == "/opt/test/bin/python" for argv, _ in calls)
    assert (
        profile.ruler.environment
        is profile.build_gate.environment
        is profile.bug_runner.environment
        is profile.environment
    )
    assert "/opt/test/bin/python" in profile.bug_runner.agent_test_hint(tmp_path)


def test_failed_readiness_blocks_discovery(tmp_path, monkeypatch):
    profile, _ = make_profile(tmp_path, monkeypatch, [FileNotFoundError("missing interpreter")])
    discovery = Mock()
    monkeypatch.setattr(gp.agent_discovery, "discover_surfaces_via_agent", discovery)
    with pytest.raises(RuntimeError, match="test environment is not ready"):
        profile.discover(base_sha="base", top_k=[], known_loci=[], agent_runner=Mock())
    discovery.assert_not_called()


def test_supervisor_checks_after_checkout_before_agent(tmp_path, monkeypatch):
    events = []
    for name in ("_repository_is_safe", "_push_disabled"):
        monkeypatch.setattr(runner.clone_setup, name, lambda path: True)

    def checkout(*args):
        events.append("checkout")
        return True, "ready"

    monkeypatch.setattr(runner.clone_setup, "checkout_branch", checkout)

    def fail():
        events.append("readiness")
        raise RuntimeError("test environment is not ready")

    profile = SimpleNamespace(
        isolation=SimpleNamespace(push_disabled=lambda: True), require_environment=fail
    )
    supervisor = runner.RunSupervisor()
    agent = Mock()
    monkeypatch.setattr(supervisor, "_build_runner", agent)
    with pytest.raises(RuntimeError, match="test environment"):
        supervisor._build_driver_locked(
            {"clone": str(tmp_path)}, lambda config: profile, Mock(), Mock()
        )
    assert events == ["checkout", "readiness"]
    agent.assert_not_called()


def test_diagnostic_redacted_before_bound_and_cleared(tmp_path, monkeypatch):
    profile, _ = make_profile(
        tmp_path,
        monkeypatch,
        [(2, "x" * 3998 + "SECRET", "import error"), (0, "1 test collected", "")],
    )
    seen = []

    def redact(text):
        seen.append(text)
        return text.replace("SECRET", "[redacted]")

    monkeypatch.setattr(gp, "redact_via_context", redact)
    assert profile.bug_runner.test_collects(src=tmp_path, test_path="test_x.py") is False
    diagnostic = profile.bug_runner.diagnostic
    assert any("SECRET" in text for text in seen)
    assert len(diagnostic["stdout"]) <= 4000
    assert diagnostic["stderr"] == "import error"
    assert profile.bug_runner.test_collects(src=tmp_path, test_path="test_x.py") is True
    assert profile.bug_runner.diagnostic is None


def test_bug_gate_includes_concrete_diagnostic(tmp_path, monkeypatch):
    profile, _ = make_profile(tmp_path, monkeypatch, [(1, "syntax failure", "bad syntax")])
    candidate = Candidate(
        kind="bug",
        target="x.py",
        reproducing_test=BugReproducingTest(test_id="test_x.py", test_path="test_x.py"),
    )
    result = BugGate().run(
        runner=profile.bug_runner, candidate=candidate, base_src=tmp_path, cand_src=tmp_path
    )
    assert not result.passed
    assert "bad syntax" in result.detail
    assert '"returncode": 1' in result.detail


def test_environment_is_repository_wide_and_restored():
    config: dict[str, Any] = {
        "target_url": "https://github.com/owner/one",
        "branch": "main",
        "testEnvironment": {"kind": "python", "pythonExecutable": "/env/bin/python"},
    }
    store.remember_test_environment(config)
    config["branch"] = "feature"
    store.restore_test_environment(config)
    assert config["testEnvironment"]["kind"] == "python"
    config["target_url"] = "https://github.com/owner/two"
    store.restore_test_environment(config)
    assert config["testEnvironment"] == {"kind": "gateway"}
    config["target_url"] = "https://github.com/owner/one.git"
    store.restore_test_environment(config)
    assert config["testEnvironment"]["pythonExecutable"] == "/env/bin/python"


def request(body):
    req = make_mocked_request("PUT", "/config")

    req._read_bytes = json.dumps(body).encode("utf-8")
    return req


@pytest.mark.asyncio
async def test_invalid_patch_preserves_all_config(monkeypatch):
    original = {"branch": "main", "testEnvironment": {"kind": "gateway"}}
    store.write_json_atomic(store.config_path(), original)
    response = await routes._handle_put_config(
        request({"branch": "other", "testEnvironment": {"kind": "shell"}})
    )
    assert response.status == 400
    assert store.read_json(store.config_path()) == original


@pytest.mark.asyncio
async def test_active_run_recheck_preserves_config(monkeypatch):
    original = {"branch": "main"}
    store.write_json_atomic(store.config_path(), original)

    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: True)
    response = await routes._handle_put_config(request({"testEnvironment": {"kind": "gateway"}}))
    assert response.status == 409
    assert store.read_json(store.config_path()) == original


@pytest.mark.asyncio
async def test_check_endpoint_rechecks_active_run(monkeypatch):
    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: True)
    response = await routes._handle_environment_check(request({}))
    assert response.status == 409


def test_xdist_probes_selected_environment(tmp_path, monkeypatch):
    profile, calls = make_profile(
        tmp_path,
        monkeypatch,
        [(3, "KIRO_XDIST_ABSENT", ""), (0, "1 passed", "")],
        {"kind": "python", "pythonExecutable": "/env/bin/python"},
    )
    assert profile.bug_runner.run_suite(src=tmp_path) == (True, [])
    assert calls[0][0][:2] == ["/env/bin/python", "-c"]
    assert "import xdist" in calls[0][0][2]
    assert "-n" not in calls[1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override", [None, {"kind": "python", "pythonExecutable": "/other/bin/python"}]
)
async def test_check_uses_selected_environment_without_saving(tmp_path, monkeypatch, override):
    from .. import profiles

    cfg = {"clone": str(tmp_path), "branch": "feature", "testEnvironment": {"kind": "gateway"}}
    store.write_json_atomic(store.config_path(), cfg)
    events = []

    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    monkeypatch.setattr(routes.clone_setup, "_repository_is_safe", lambda path: True)
    monkeypatch.setattr(routes.clone_setup, "_push_disabled", lambda path: True)

    def checkout(path, branch):
        events.append(branch)
        return True, "ready"

    monkeypatch.setattr(routes.clone_setup, "checkout_branch", checkout)

    def build(config):
        assert events == ["feature"]
        assert config["testEnvironment"] == (override or cfg["testEnvironment"])
        return SimpleNamespace(
            environment=SimpleNamespace(identity=config["testEnvironment"]),
            isolation=SimpleNamespace(push_disabled=lambda: True),
            check_environment=lambda: {
                "ok": True,
                "environment": config["testEnvironment"],
                "diagnostic": None,
                "tests_collected": 7,
            },
        )

    monkeypatch.setattr(profiles, "build_profile", build)
    response = await routes._handle_environment_check(
        request({"testEnvironment": override} if override else {})
    )
    assert response.status == 200
    assert json.loads(response.text)["tests_collected"] == 7
    assert store.read_json(store.config_path()) == cfg


@pytest.mark.asyncio
async def test_setup_switch_restores_repository_environment(tmp_path, monkeypatch):
    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    monkeypatch.setattr(
        routes.clone_setup,
        "setup_safe_clone",
        lambda url, path: (
            {
                "clone": str(tmp_path),
                "display": url.rsplit("/", 1)[-1],
                "push_disabled": True,
            },
            "",
        ),
    )
    original = {
        "target_url": "https://github.com/owner/one",
        "branch": "feature",
        "testEnvironment": {"kind": "python", "pythonExecutable": "/one/bin/python"},
    }
    store.write_json_atomic(store.config_path(), original)
    response = await routes._handle_setup_clone(request({"url": "https://github.com/owner/two"}))
    assert response.status == 200
    assert store.read_json(store.config_path())["testEnvironment"] == {"kind": "gateway"}
    response = await routes._handle_setup_clone(request({"url": "https://github.com/owner/one"}))
    assert response.status == 200
    assert store.read_json(store.config_path())["testEnvironment"] == original["testEnvironment"]


def test_direct_calibration_refuses_before_samples(tmp_path, monkeypatch):
    profile, calls = make_profile(tmp_path, monkeypatch, [(1, "", "interpreter refused")])
    workload = Mock()
    monkeypatch.setattr(profile.ruler, "_time_once", workload)
    with pytest.raises(RuntimeError, match="test environment"):
        profile.ruler.baseline_samples(base_src=tmp_path, reps=3)
    workload.assert_not_called()


def test_all_python_adapters_use_selected_interpreter(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "1 test collected", "")

    monkeypatch.setattr(gp, "_run", run)
    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        test_environment={"kind": "python", "pythonExecutable": "/selected/bin/python"},
        benchmark_cmd="python -m benchmark",
    )
    assert profile.bug_runner.build_imports_ok(src=tmp_path)
    assert profile.bug_runner.lint_clean(base_src=tmp_path, cand_src=tmp_path)
    assert profile.bug_runner.test_collects(src=tmp_path, test_path="test_x.py")
    assert profile.bug_runner.run_reproducing_test(
        src=tmp_path, test_id="test_x.py::test_x", test_only=False
    )
    assert profile.build_gate.build_and_test(worktree=tmp_path, src=tmp_path).passed
    assert profile.ruler._time_once(tmp_path)[1]
    assert profile.ruler._time_once(tmp_path, collect_only=True)[1]
    assert gp._collected_count(tmp_path, profile.environment) == 1
    assert all(argv[0] == "/selected/bin/python" for argv in calls)
    assert any("benchmark" in argv for argv in calls)


def test_runner_profiler_exports_from_temporary_storage(tmp_path, monkeypatch):
    import base64

    from ..backend import profile_normalize

    profile = gp.GitHubRepoProfile(
        clone_path=tmp_path,
        pr_queue_dir=tmp_path / "queue",
        test_environment={"kind": "gateway"},
    )

    profile.environment._config = {"kind": "runner", "pythonExecutable": "python"}

    def run(argv, **kwargs):
        assert argv[:2] == ["python", "-c"]
        compile(argv[2], "profiler", "exec")
        assert str(store.profiles_dir()) not in argv[2]
        return subprocess.CompletedProcess(
            argv, 0, "KIRO_PSTATS:" + base64.b64encode(b"profile-data").decode(), ""
        )

    monkeypatch.setattr(profile.environment, "run", run)
    monkeypatch.setattr(
        profile_normalize, "capture_profile", lambda fp, raw, **kwargs: {"data": raw.read_bytes()}
    )
    assert profile.capture_profile(fp="test", worktree=tmp_path) == {"data": b"profile-data"}


def test_timeout_diagnostic_retains_both_streams(tmp_path, monkeypatch):
    error = subprocess.TimeoutExpired(
        "pytest", 1, output=b"collected output", stderr=b"timeout detail"
    )
    profile, _ = make_profile(tmp_path, monkeypatch, [error])
    assert not profile.bug_runner.test_collects(src=tmp_path, test_path="test_x.py")
    assert profile.bug_runner.diagnostic == {
        "stage": "collect",
        "returncode": None,
        "stdout": "collected output",
        "stderr": "timeout detail",
    }


@pytest.mark.parametrize(
    "failure",
    [
        RunnerAdmissionError("runner denied"),
        RuntimeError("execution admission refused"),
        PermissionError("execution denied"),
        subprocess.TimeoutExpired("xdist probe", 1),
        subprocess.SubprocessError("execution failed"),
        (1, "", "ModuleNotFoundError: No module named 'xdist_dependency'"),
        (125, "", "runner could not start"),
    ],
)
@pytest.mark.parametrize("adapter", ["bug_runner", "build_gate"])
def test_xdist_failure_stops_before_suite(tmp_path, monkeypatch, failure, adapter):
    profile, calls = make_profile(tmp_path, monkeypatch, [failure])
    target = getattr(profile, adapter)
    with pytest.raises((RuntimeError, OSError, subprocess.SubprocessError)):
        if adapter == "bug_runner":
            target.run_suite(src=tmp_path)
        else:
            target.build_and_test(worktree=tmp_path, src=tmp_path)
    assert len(calls) == 1
    assert target.diagnostic["stage"] == "xdist"
    assert target.diagnostic["stderr"]


@pytest.mark.parametrize(
    "operation",
    [
        lambda p, root: p.ruler.measure(
            base_src=root, cand_src=root, commit_sha="base", scenario="suite"
        ),
        lambda p, root: p.ruler.measure_canary(base_src=root),
        lambda p, root: p.ruler.baseline_samples(base_src=root, reps=3),
        lambda p, root: gp._collected_count(root, p.environment),
        lambda p, root: p.bug_runner.lint_clean(base_src=root, cand_src=root),
        lambda p, root: p.bug_runner.build_imports_ok(src=root),
        lambda p, root: p.bug_runner.test_collects(src=root, test_path="test_x.py"),
        lambda p, root: p.bug_runner.run_reproducing_test(
            src=root, test_id="test_x.py::test_x", test_only=False
        ),
        lambda p, root: p.bug_runner.run_suite(src=root),
        lambda p, root: p.build_gate.build_and_test(worktree=root, src=root),
        lambda p, root: p.capture_profile(fp="cleanup", worktree=root),
    ],
)
def test_admission_failure_aborts_following_workloads(tmp_path, monkeypatch, operation):
    error = RunnerAdmissionError("runner denied")
    profile, calls = make_profile(tmp_path, monkeypatch, [error])
    monkeypatch.setattr(profile.ruler, "require_environment", lambda: None)
    monkeypatch.setattr(profile.bug_runner, "_xdist", lambda root: ("-n", "auto"))
    monkeypatch.setattr(profile.build_gate, "_xdist", lambda root: ("-n", "auto"))
    with pytest.raises(RunnerAdmissionError, match="runner denied"):
        operation(profile, tmp_path)
    assert len(calls) == 1


def test_readiness_admission_failure_stops_and_retains_diagnostic(tmp_path, monkeypatch):
    error = RunnerAdmissionError("runner denied")
    profile, calls = make_profile(tmp_path, monkeypatch, [error])
    result = profile.check_environment()
    assert result["ok"] is False
    assert result["diagnostic"]["stderr"] == str(error)
    assert len(calls) == 1


@pytest.mark.parametrize("adapter", ["bug_runner", "build_gate"])
def test_serial_retry_admission_failure_is_fatal(tmp_path, monkeypatch, adapter):
    error = RunnerAdmissionError("runner denied")
    profile, calls = make_profile(
        tmp_path, monkeypatch, [(0, "", ""), (1, "worker crashed", ""), error]
    )
    target = getattr(profile, adapter)
    with pytest.raises(RunnerAdmissionError, match="runner denied"):
        if adapter == "bug_runner":
            target.run_suite(src=tmp_path)
        else:
            target.build_and_test(worktree=tmp_path, src=tmp_path)
    assert len(calls) == 3
    assert "-n" in calls[1][0]
    assert "-n" not in calls[2][0]
    assert target.diagnostic["stderr"] == str(error)


def test_benchmark_admission_failure_stops_measurement(tmp_path, monkeypatch):
    error = RunnerAdmissionError("runner denied")
    profile, calls = make_profile(tmp_path, monkeypatch, [error])
    profile.ruler.benchmark_cmd = "python -m benchmark"
    with pytest.raises(RunnerAdmissionError, match="runner denied"):
        profile.ruler.measure(
            base_src=tmp_path, cand_src=tmp_path, commit_sha="base", scenario="benchmark"
        )
    assert len(calls) == 1


@pytest.mark.parametrize("command", ["pytest -q", "python3.12 -m pytest -q"])
def test_custom_benchmark_uses_selected_python(tmp_path, monkeypatch, command):
    profile, calls = make_profile(
        tmp_path,
        monkeypatch,
        [(0, "", "")],
        {"kind": "python", "pythonExecutable": "/selected/venv/bin/python"},
    )
    profile.ruler.benchmark_cmd = command
    assert profile.ruler._time_once(tmp_path)[1]
    assert calls[0][0] == ["/selected/venv/bin/python", "-m", "pytest", "-q"]


def test_custom_benchmark_refuses_ambiguous_launcher(tmp_path, monkeypatch):
    profile, calls = make_profile(tmp_path, monkeypatch, [])
    profile.ruler.benchmark_cmd = "env python -m pytest"
    with pytest.raises(RuntimeError, match="selected test environment"):
        profile.ruler._time_once(tmp_path)
    assert not calls


@pytest.mark.parametrize(
    "reply",
    [
        (2, "", "invalid Ruff configuration"),
        (1, "", "No module named missing_internal_dependency"),
    ],
)
def test_linter_execution_failure_cannot_pass_gate(tmp_path, monkeypatch, reply):
    profile, calls = make_profile(tmp_path, monkeypatch, [reply])
    with pytest.raises(RuntimeError, match="ruff"):
        profile.bug_runner.lint_clean(base_src=tmp_path, cand_src=tmp_path)
    assert len(calls) == 1
    assert profile.bug_runner.diagnostic["stderr"] == reply[2]
