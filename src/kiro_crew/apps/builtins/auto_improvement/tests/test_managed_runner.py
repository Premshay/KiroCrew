"""An app-managed runner is a pinned commit the app extracts and rebuilds on its own."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup, runner_source, store
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import environment as env_mod
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import pr_recipe

_RUNNER = "scripts/dev/run_suite.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_runner(work: Path, body: str) -> str:
    script = work / _RUNNER
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", body)
    _git(work, "push", "-q", "origin", "HEAD:main")
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("AUTO_IMPROVEMENT_RUNNERS", str(tmp_path / "runners"))
    monkeypatch.setattr(clone_setup, "resolve_origin_url", lambda _config: remote.as_uri())
    monkeypatch.setattr(pr_recipe, "_prefer_authenticated_remote", lambda url: url)
    return work


def _config(commit: str = "") -> dict:
    environment = {"kind": "runner", "runnerPath": _RUNNER}
    if commit:
        environment["runnerCommit"] = commit
    return {
        "target_url": "https://github.com/acme/widgets",
        "branch": "origin/main",
        "testEnvironment": environment,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "runner", "runnerPath": "/abs/run.sh"},
        {"kind": "runner", "runnerPath": "../outside/run.sh"},
        {"kind": "runner", "runnerPath": "scripts/./run.sh"},
        {"kind": "runner", "runnerPath": ""},
        {"kind": "runner", "runnerPath": "run.sh", "runnerExecutable": "/opt/run.sh"},
        {"kind": "runner", "runnerPath": "run.sh", "runnerCommit": "abc123"},
        {"kind": "runner", "runnerExecutable": "/opt/run.sh", "runnerCommit": "a" * 40},
        {"kind": "gateway", "runnerPath": "run.sh"},
    ],
)
def test_rejects_invalid_managed_runner_settings(value):
    with pytest.raises(ValueError):
        env_mod.normalize_test_environment(value)


def test_saving_pins_the_branch_and_extracts_a_read_only_snapshot(upstream):
    head = _commit_runner(upstream, "echo one")
    config = _config()
    note = runner_source.prepare_runner(config, pin=True)

    assert config["testEnvironment"]["runnerCommit"] == head
    assert note.startswith("pinned")
    executable = runner_source.managed_runner_executable(config["testEnvironment"])
    assert executable.read_text() == "#!/bin/sh\necho one\n"
    assert os.access(executable, os.X_OK)
    assert not os.access(executable, os.W_OK)
    assert not os.access(executable.parent, os.W_OK)
    identity = env_mod.TestEnvironment(
        config["testEnvironment"], upstream.parent / "clones" / "repo", None
    ).identity
    assert identity["runnerCommit"] == head


def test_the_snapshot_is_a_self_contained_checkout_of_the_pin(upstream):
    head = _commit_runner(upstream, "echo one")
    config = _config()
    runner_source.prepare_runner(config, pin=True)
    root = runner_source.managed_runner_executable(config["testEnvironment"]).parents[2]

    assert _git(root, "rev-parse", "HEAD") == head
    assert _RUNNER in _git(root, "ls-tree", "-r", "--name-only", head).splitlines()
    mirror = str(store.data_dir())
    for path in (root / ".git").rglob("*"):
        if path.is_file():
            assert mirror.encode() not in path.read_bytes(), path


def test_a_run_never_pins_an_unpinned_runner(upstream):
    _commit_runner(upstream, "echo one")
    with pytest.raises(env_mod.RunnerAdmissionError, match="not pinned"):
        runner_source.prepare_runner(_config())


def test_a_removed_snapshot_is_rebuilt_from_the_pin_not_the_branch(upstream):
    pinned = _commit_runner(upstream, "echo one")
    config = _config()
    runner_source.prepare_runner(config, pin=True)
    _commit_runner(upstream, "echo two")
    snapshot = runner_source.managed_runner_executable(config["testEnvironment"]).parents[2]
    runner_source._remove(snapshot)

    note = runner_source.prepare_runner(config)

    assert note.startswith("rebuilt")
    assert config["testEnvironment"]["runnerCommit"] == pinned
    executable = runner_source.managed_runner_executable(config["testEnvironment"])
    assert executable.read_text() == "#!/bin/sh\necho one\n"


def test_a_pinned_commit_missing_from_the_mirror_is_fetched(upstream):
    pinned = _commit_runner(upstream, "echo one")
    config = _config(pinned)

    runner_source.prepare_runner(config)

    assert runner_source.managed_runner_executable(config["testEnvironment"]).is_file()


def test_a_runner_path_absent_at_the_commit_is_refused(upstream):
    _commit_runner(upstream, "echo one")
    config = _config()
    config["testEnvironment"]["runnerPath"] = "scripts/dev/missing.sh"
    with pytest.raises(env_mod.RunnerAdmissionError, match="not a regular file"):
        runner_source.prepare_runner(config, pin=True)


def test_prune_keeps_only_referenced_snapshots(upstream):
    first = _commit_runner(upstream, "echo one")
    old = _config()
    runner_source.prepare_runner(old, pin=True)
    _commit_runner(upstream, "echo two")
    new = _config()
    runner_source.prepare_runner(new, pin=True)
    snapshots = store.runner_snapshot_path()
    assert len(list(snapshots.iterdir())) == 2

    (snapshots / "notes.txt").write_text("not a snapshot")
    runner_source.prune_snapshots(new)

    assert sorted(p.name for p in snapshots.iterdir()) == sorted(
        [new["testEnvironment"]["runnerCommit"], "notes.txt"]
    )
    assert first != new["testEnvironment"]["runnerCommit"]


@pytest.mark.parametrize(
    "environment",
    [
        {"kind": "runner", "runnerExecutable": "/nonexistent/recipes/run_suite.sh"},
        {"kind": "runner", "runnerPath": _RUNNER, "runnerCommit": "b" * 40},
    ],
)
def test_a_missing_runner_is_an_admission_error_not_a_crash(tmp_path, monkeypatch, environment):
    monkeypatch.setenv("AUTO_IMPROVEMENT_RUNNERS", str(tmp_path / "runners"))
    with pytest.raises(env_mod.RunnerAdmissionError, match="Runner not found"):
        env_mod.TestEnvironment(environment, tmp_path / "clones" / "repo", None)


def _put(body: dict):
    import json

    from .request_helpers import owner_request

    req = owner_request("PUT", "/config")
    req._read_bytes = json.dumps(body).encode("utf-8")
    return req


@pytest.fixture
def config_route(monkeypatch):
    from kiro_crew.apps.builtins.auto_improvement.backend import routes

    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    return routes


@pytest.mark.asyncio
async def test_saving_the_environment_pins_and_remembers_the_runner(upstream, config_route):
    head = _commit_runner(upstream, "echo one")
    store.write_json_atomic(
        store.config_path(), {**_config(), "testEnvironment": {"kind": "gateway"}}
    )

    response = await config_route._handle_put_config(
        _put({"testEnvironment": {"kind": "runner", "runnerPath": _RUNNER}})
    )

    assert response.status == 200
    saved = store.read_json(store.config_path())
    assert saved["testEnvironment"]["runnerCommit"] == head
    assert head in {e.get("runnerCommit") for e in saved["repositoryTestEnvironments"].values()}
    assert runner_source.managed_runner_executable(saved["testEnvironment"]).is_file()


@pytest.mark.asyncio
async def test_a_runner_that_cannot_be_pinned_leaves_config_unchanged(upstream, config_route):
    original = {**_config(), "testEnvironment": {"kind": "gateway"}}
    store.write_json_atomic(store.config_path(), original)

    response = await config_route._handle_put_config(
        _put({"testEnvironment": {"kind": "runner", "runnerPath": _RUNNER}})
    )

    assert response.status == 400
    assert store.read_json(store.config_path()) == original


def test_snapshots_are_not_masked_from_the_measurement_sandbox(monkeypatch):
    from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile_execution import (
        _write_protected_targets,
    )

    monkeypatch.delenv("AUTO_IMPROVEMENT_RUNNERS", raising=False)
    root = store.runner_snapshot_path()
    masked = [Path(target) for target in _write_protected_targets()]
    assert masked, "the executor masks the crew home; this test needs that list"
    assert not any(root.is_relative_to(target) for target in masked)
    assert not root.is_relative_to(store.scratch_path())
