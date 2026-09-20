from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import environment as mod
from kiro_crew.atomic_write import atomic_write


@pytest.mark.parametrize(
    "config",
    [
        [],
        "python -m pytest",
        {"kind": "shell"},
        {"kind": []},
        {"command": "pytest"},
        {"kind": "python", "pythonExecutable": "python"},
        {"kind": "runner", "runnerExecutable": "/runner", "pythonExecutable": "python -c evil"},
        {"kind": "python", "pythonExecutable": "/usr/bin/python\n"},
        {"kind": "gateway", "image": "python"},
        {"kind": "gateway", "pythonExecutable": "/x"},
        {"kind": "container", "image": "--privileged"},
        {"kind": "container", "image": "python;id"},
        *(
            {"kind": "container", "image": "python", "network": n}
            for n in ["host", "bridge", "default", "container:live", "--host"]
        ),
        *(
            {"variables": {key: "x"}}
            for key in [
                "AWS_SECRET_ACCESS_KEY",
                "TOKEN",
                "PASSWORD",
                "SSH_AUTH_SOCK",
                "PATH",
                "PYTHONPATH",
                "LD_PRELOAD",
                "KIROCREW_RUNNER_DEADLINE",
                "KIROCREW_RUNNER_CLEANUP_SECONDS",
            ]
        ),
        {"variables": {"TEST": "$(id)"}},
        {"variables": {"TEST": 1}},
    ],
)
def test_rejects_invalid_config(config):
    with pytest.raises(ValueError):
        mod.normalize_test_environment(config)


def test_gateway_default_and_lexical_python(tmp_path):
    assert mod.normalize_test_environment(None) == {"kind": "gateway"}
    target = tmp_path / "python"
    target.symlink_to(sys.executable)
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    adapter = mod.TestEnvironment(
        {"kind": "python", "pythonExecutable": str(target), "variables": {"TEST_MODE": "yes"}},
        tmp_path,
        runner,
    )
    assert adapter.python_argv("-m", "pytest") == [str(target), "-m", "pytest"]
    adapter.run(adapter.python_argv(), cwd=tmp_path, timeout=2, env={"LANG": "C"})
    runner.assert_called_once_with(
        [str(target)], cwd=tmp_path, timeout=2, env={"LANG": "C", "TEST_MODE": "yes"}
    )
    identity = adapter.identity
    identity["variables"]["TEST_MODE"] = "changed"
    assert adapter.identity["variables"]["TEST_MODE"] == "yes"
    json.dumps(adapter.identity)


def test_python_path_with_spaces_is_forwarded_as_one_argument(tmp_path):
    executable = str(tmp_path / "Test Environment" / "python")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    adapter = mod.TestEnvironment({"kind": "python", "pythonExecutable": executable}, tmp_path, run)
    adapter.run(adapter.python_argv("-c", "pass"), cwd=tmp_path, timeout=5)
    assert run.call_args.args[0] == [executable, "-c", "pass"]


def test_native_windows_python_path(monkeypatch):
    from pathlib import PureWindowsPath

    monkeypatch.setattr(mod, "Path", PureWindowsPath)
    executable = r"C:\Program Files\Python312\python.exe"
    assert (
        mod.normalize_test_environment({"kind": "python", "pythonExecutable": executable})[
            "pythonExecutable"
        ]
        == executable
    )


@pytest.fixture
def repository_runner(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from kiro_crew import sel as sel_module
    from kiro_crew.platform import context, governance_profiles
    from kiro_crew.platform.governance import parse_policy

    from ..backend import store

    if os.name == "nt":
        pytest.skip("executable script fixture uses a POSIX shebang")
    clone = tmp_path / "managed" / "clone"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True, timeout=10)
    executable = tmp_path / "operator runner"
    atomic_write(
        executable,
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1] == '--source' and sys.argv[3] == '--'\n"
        "assert os.getcwd() == sys.argv[2]\n"
        "os.execv(sys.argv[4], sys.argv[4:])\n",
        mode=0o700,
    )
    config = {
        "kind": "runner",
        "runnerExecutable": str(executable),
        "pythonExecutable": sys.executable,
    }
    store.write_json_atomic(store.config_path(), {"clone": str(clone), "testEnvironment": config})
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile_path = profiles / "test.json"
    profile_data = {"name": "test", "bind": {"type": "app", "id": store.APP_NAME}}
    profile_path.write_text(json.dumps(profile_data), encoding="utf-8")
    monkeypatch.setattr(governance_profiles, "_PROFILES_DIR", profiles)
    governance_profiles.reset_store()
    ctx = SimpleNamespace(governance=parse_policy({"version": 1, "boot": {}}))
    monkeypatch.setattr(context, "current_context", lambda: ctx)
    monkeypatch.setattr(mod, "current_context", lambda: ctx)
    audit = Mock()
    monkeypatch.setattr(sel_module, "sel", lambda: audit)
    calls = []

    def sandbox_run(argv, **kwargs):
        calls.append((argv, kwargs))
        kwargs["env"] = {"PATH": os.environ.get("PATH", ""), **(kwargs.get("env") or {})}
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", shell=False, **kwargs
        )

    adapter = mod.TestEnvironment(config, clone, sandbox_run)
    yield SimpleNamespace(
        adapter=adapter,
        clone=clone,
        executable=executable,
        calls=calls,
        ctx=ctx,
        audit=audit,
        config=config,
        profile_path=profile_path,
        profile_data=profile_data,
    )
    governance_profiles.reset_store()


def authenticated_run(state, *arguments, cwd=None, timeout=5, principal=None, env=None):
    import asyncio

    from kiro_crew.platform.app_execution import authenticated_app_execution

    @authenticated_app_execution("auto-improvement")
    async def handler(request):
        return state.adapter.run(
            state.adapter.python_argv(*arguments), cwd=cwd or state.clone, timeout=timeout, env=env
        )

    return asyncio.run(
        handler(
            {"user": "operator", "app": "", "is_dashboard_user": True}
            if principal is None
            else principal
        )
    )


def test_runner_forwards_literal_argv_and_exact_linked_checkout(repository_runner):
    state = repository_runner
    (state.clone / "marker").write_text("base", encoding="utf-8")
    for args in (["add", "."], ["commit", "-m", "fixture"]):
        subprocess.run(
            ["git", "-C", str(state.clone), *args], check=True, capture_output=True, timeout=10
        )
    candidate = state.clone.parent / "candidate"
    subprocess.run(
        ["git", "-C", str(state.clone), "worktree", "add", "--detach", str(candidate)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    (candidate / "marker").write_text("candidate", encoding="utf-8")
    code = "import json,os,pathlib,sys; print(json.dumps([pathlib.Path('marker').read_text(),sys.argv[1:],os.getenv('TEST_MODE')]))"
    state.adapter._config["variables"] = {"TEST_MODE": "selected"}
    for source, value in [(state.clone, "base"), (candidate, "candidate")]:
        result = authenticated_run(state, "-c", code, "literal ; $(no-shell)", cwd=source)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [value, ["literal ; $(no-shell)"], "selected"]
        assert state.calls[-1][0][:5] == [
            str(state.executable),
            "--source",
            str(source),
            "--",
            sys.executable,
        ]
    assert len(state.adapter.identity["runnerIdentity"]["sha256"]) == 64


@pytest.mark.parametrize(
    "principal",
    [{}, {"user": "operator"}, {"user": "operator", "app": "other", "is_dashboard_user": False}],
)
def test_runner_requires_authenticated_provenance(repository_runner, principal):
    with pytest.raises(mod.RunnerAdmissionError, match="authenticated"):
        authenticated_run(repository_runner, "-c", "pass", principal=principal)
    assert not repository_runner.calls


@pytest.mark.parametrize("layer", ["policy", "profile"])
@pytest.mark.parametrize("scope", ["apps", "commands", "filesystem.read"])
def test_runner_real_governance_denials(repository_runner, layer, scope):
    from kiro_crew.platform import governance_profiles
    from kiro_crew.platform.governance import parse_policy

    state = repository_runner
    key, _, subkey = scope.partition(".")
    rule = {"mode": "deny", "deny": ["*"]}
    controls = {key: {subkey: rule} if subkey else rule}
    if layer == "policy":
        state.ctx.governance = parse_policy({"version": 1, "boot": {}, **controls})
    else:
        state.profile_path.write_text(
            json.dumps({**state.profile_data, **controls}), encoding="utf-8"
        )
        governance_profiles.reset_store()
    with pytest.raises(mod.RunnerAdmissionError, match=scope):
        authenticated_run(state, "-c", "pass")
    assert not state.calls


def test_runner_cannot_bypass_inner_command_denial(repository_runner):
    from kiro_crew.platform.governance import parse_policy

    state = repository_runner
    state.ctx.governance = parse_policy(
        {"version": 1, "boot": {}, "commands": {"mode": "deny", "deny": [sys.executable + " *"]}}
    )
    with pytest.raises(mod.RunnerAdmissionError, match="commands"):
        authenticated_run(state, "-c", "pass")
    assert not state.calls


@pytest.mark.parametrize("scope", ["filesystem.read", "filesystem.write", "network.egress"])
def test_runner_does_not_claim_to_enforce_delegated_policy(repository_runner, scope):
    from kiro_crew.platform.governance import parse_policy

    key, subkey = scope.split(".")
    state = repository_runner
    state.ctx.governance = parse_policy(
        {"version": 1, "boot": {}, key: {subkey: {"mode": "deny", "deny": ["unrelated"]}}}
    )
    with pytest.raises(mod.RunnerAdmissionError, match="cannot enforce delegated " + scope):
        authenticated_run(state, "-c", "pass")
    assert not state.calls


@pytest.mark.parametrize("mutation", ["content", "replacement", "in_checkout", "audit", "clone"])
def test_runner_identity_and_admission_fail_closed(repository_runner, mutation):
    from ..backend import store

    state = repository_runner
    if mutation == "content":
        state.executable.write_text("changed", encoding="utf-8")
    elif mutation == "replacement":
        data = state.executable.read_bytes()
        state.executable.rename(state.executable.with_suffix(".old"))
        atomic_write(state.executable, data, mode=0o700)
    elif mutation == "in_checkout":
        executable = state.clone / "runner"
        atomic_write(executable, state.executable.read_bytes(), mode=0o700)
        state.adapter._config["runnerExecutable"] = str(executable)
    elif mutation == "audit":
        state.audit.log_governance_decision.side_effect = OSError("audit unavailable")
    else:
        store.write_json_atomic(store.config_path(), {"clone": "another"})
    with pytest.raises(mod.RunnerAdmissionError):
        authenticated_run(state, "-c", "pass")
    assert not state.calls


def test_runner_tampering_during_workload_rejects_result(repository_runner):
    state = repository_runner
    with pytest.raises(mod.RunnerAdmissionError, match="identity changed"):
        authenticated_run(
            state,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('changed')",
            str(state.executable),
        )


def test_real_runner_exit_and_timeout(repository_runner, monkeypatch):
    monkeypatch.setattr(mod, "_RUNNER_CLEANUP_SECONDS", 0.1)
    result = authenticated_run(
        repository_runner,
        "-c",
        "import sys; print('out'); print('failure', file=sys.stderr); sys.exit(7)",
    )
    assert (result.returncode, result.stdout, result.stderr) == (7, "out\n", "failure\n")
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        authenticated_run(
            repository_runner,
            "-c",
            "import time; print('started', flush=True); time.sleep(60)",
            timeout=0.5,
        )
    assert b"started" in caught.value.output


def test_runner_can_cleanup_between_work_deadline_and_hard_timeout(repository_runner):
    state = repository_runner
    receipt = state.clone / "cleanup-receipt"
    state.executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, time\n"
        "deadline = float(os.environ['KIROCREW_RUNNER_DEADLINE'])\n"
        "time.sleep(max(0, deadline - time.time()))\n"
        "time.sleep(0.05)\n"
        "pathlib.Path(sys.argv[2], 'cleanup-receipt').write_text('cleaned')\n"
        "sys.exit(124)\n",
        encoding="utf-8",
    )
    state.adapter = mod.TestEnvironment(state.config, state.clone, state.adapter._sandbox_run)
    result = authenticated_run(state, "-c", "pass", timeout=0.2)
    assert result.returncode == 124
    assert receipt.read_text(encoding="utf-8") == "cleaned"
    assert state.calls[-1][1]["timeout"] == 30.2
    assert state.calls[-1][1]["env"]["KIROCREW_RUNNER_CLEANUP_SECONDS"] == "30"


def test_legacy_container_has_actionable_error():
    with pytest.raises(ValueError, match="select a repository runner"):
        mod.normalize_test_environment({"kind": "container", "image": "old"})
    assert mod.normalize_test_environment(
        {"kind": "runner", "runnerExecutable": "/opt/recipe"}
    ) == {"kind": "runner", "runnerExecutable": "/opt/recipe", "pythonExecutable": "python"}


@pytest.mark.asyncio
async def test_every_verification_path_uses_admitted_runner_and_strict_executor(
    repository_runner, monkeypatch
):
    from kiro_crew.platform.app_execution import authenticated_app_execution

    from ..profiles.github_repo import profile as gp

    state = repository_runner
    launched = []

    def sandbox(argv, **kwargs):
        assert kwargs["mode"] == "strict"
        assert kwargs["extra_visible_dirs"] == (str(state.clone),)
        return argv, {"LANG": "C"}, None

    def limited(argv, **kwargs):
        assert argv[:5] == [
            str(state.executable),
            "--source",
            str(state.clone),
            "--",
            sys.executable,
        ]
        assert kwargs["shell"] is False
        assert kwargs["env"]["PYTHONPATH"] == ""
        assert kwargs["timeout"] > 0
        launched.append(argv[5:])
        if "import xdist" in " ".join(argv[5:]):
            return subprocess.CompletedProcess(argv, 3, "KIRO_XDIST_ABSENT", "")
        return subprocess.CompletedProcess(argv, 0, "1 test collected", "")

    monkeypatch.setattr(gp, "sandboxed_spawn_argv", sandbox)
    monkeypatch.setattr(gp, "run_limited", limited)
    monkeypatch.setattr(gp, "_write_protected_targets", lambda: ())
    profile = gp.GitHubRepoProfile(
        clone_path=state.clone,
        pr_queue_dir=state.clone.parent / "queue",
        test_environment=state.config,
        benchmark_cmd="python -m benchmark",
    )

    @authenticated_app_execution("auto-improvement")
    async def verify(request):
        root = state.clone
        assert profile.check_environment()["ok"]
        assert profile.bug_runner.build_imports_ok(src=root)
        assert profile.bug_runner.lint_clean(base_src=root, cand_src=root)
        assert profile.bug_runner.test_collects(src=root, test_path="test_x.py")
        assert profile.bug_runner.run_reproducing_test(
            src=root, test_id="test_x.py::test_x", test_only=False
        )
        assert profile.bug_runner.run_suite(src=root)[0]
        assert profile.build_gate.build_and_test(worktree=root, src=root).passed
        assert profile.ruler._time_once(root)[1]
        assert profile.ruler._time_once(root, collect_only=True)[1]
        assert gp._collected_count(root, profile.environment) == 1
        profile.capture_profile(fp="runner", worktree=root)

    await verify({"user": "operator", "app": "", "is_dashboard_user": True})
    assert any("benchmark" in args for args in launched)
    assert any("compile(p.read_bytes()" in " ".join(args) for args in launched)
    assert any("ruff" in args for args in launched)
    assert any("cProfile" in " ".join(args) for args in launched)
    recorded = json.loads(profile.ruler.measurement_constants["environment"])
    assert recorded["runnerIdentity"] == profile.environment.identity["runnerIdentity"]


def test_runner_refuses_unrelated_checkout(repository_runner, tmp_path):
    source = repository_runner.clone.parent / "unrelated"
    source.mkdir()
    (source / ".git").mkdir()
    with pytest.raises(ValueError, match="linked worktree"):
        authenticated_run(repository_runner, "-c", "pass", cwd=source)
    assert not repository_runner.calls


def test_gateway_default_keeps_existing_executor(tmp_path):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    adapter = mod.TestEnvironment(None, tmp_path, run)
    adapter.run(adapter.python_argv("-m", "pytest"), cwd=tmp_path, timeout=5)
    run.assert_called_once_with([sys.executable, "-m", "pytest"], cwd=tmp_path, timeout=5, env=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,leak", [(0, False), (0, True), (9, False)])
async def test_repo_isolation_boot_runs_recipe_and_propagates_failure(
    repository_runner, monkeypatch, exit_code, leak
):
    from kiro_crew.platform.app_execution import authenticated_app_execution

    from ..profiles.github_repo import profile as gp
    from ..spine.pollute import run_do_not_pollute

    state = repository_runner
    receipt = state.executable.parent / "boot-receipt"
    protected = state.executable.parent / "protected"
    protected.write_text("unchanged", encoding="utf-8")
    state.executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, subprocess, sys\n"
        f"receipt = pathlib.Path({str(receipt)!r})\n"
        "receipt.write_text('started', encoding='utf-8')\n"
        "try:\n"
        "    subprocess.run(sys.argv[4:], check=True, timeout=5)\n"
        f"    if {leak!r}:\n"
        f"        pathlib.Path({str(protected)!r}).write_text('leaked', encoding='utf-8')\n"
        f"    sys.exit({exit_code})\n"
        "finally:\n"
        "    receipt.write_text('started and cleaned', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gp, "_run", state.adapter._sandbox_run)
    profile = gp.GitHubRepoProfile(
        clone_path=state.clone,
        pr_queue_dir=state.clone / "queue",
        test_environment=state.config,
    )
    assert profile.isolation.environment is profile.environment

    @authenticated_app_execution("auto-improvement")
    async def preflight(request):
        return run_do_not_pollute(paths=[protected], boot=profile.isolation.measurement_boot())

    request = {"user": "operator", "app": "", "is_dashboard_user": True}

    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as raised:
            await preflight(request)
        assert raised.value.returncode == exit_code
    else:
        result = await preflight(request)
        assert result.blocked is leak
        assert result.changed_paths == ([str(protected)] if leak else [])
    assert receipt.read_text(encoding="utf-8") == "started and cleaned"
    assert len(state.calls) == 1
    argv, kwargs = state.calls[0]
    assert argv == [
        str(state.executable),
        "--source",
        str(state.clone),
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    assert kwargs["cwd"] == state.clone
    assert kwargs["timeout"] == gp._QUICK_TIMEOUT_S + mod._RUNNER_CLEANUP_SECONDS
