"""Repository recommendations must be evidence based and never execute repo code."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend.repository_survey import (
    resolve_survey_ref,
    survey_repository,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    ).stdout.strip()


def _write(root: Path, path: str, content: str) -> None:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _commit(root: Path) -> str:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "orders/models.py", "raise RuntimeError('must not import')\n")
    _write(tmp_path, "orders/tests/test_models.py", "def test_ok(): pass\n")
    _write(tmp_path, "benchmarks/jobs.py", "raise RuntimeError('must not run')\n")
    _write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["django", "pytest"]\n')
    _write(
        tmp_path,
        "compose.yml",
        "services:\n  db:\n    image: postgres:18\n  cache:\n    image: redis:7\n",
    )
    _commit(tmp_path)
    return tmp_path


def _declare(repo, value):
    _write(repo, ".kirocrew/auto-improvement.json", json.dumps(value))
    _commit(repo)


def _perf():
    return {
        "id": "jobs",
        "label": "Speed up jobs",
        "description": "Measure fixed job data",
        "config": {
            "track": "perf",
            "benchmarkCommand": "python benchmarks/jobs.py",
            "benchmarkCanaryCommand": "python benchmarks/jobs.py --slow",
            "benchmarkProtectedPaths": ["benchmarks"],
            "editAllowlist": ["orders/**"],
            "focusedTestPaths": ["orders/tests"],
        },
    }


def test_detects_source_test_pair_and_reports_perf_as_evidence(repo):
    result = survey_repository(repo)
    assert result["revision"] == _git(repo, "rev-parse", "HEAD")
    assert result["environment"] is None
    assert result["declarationPath"] is None
    assert result["workloads"] == [
        {
            "id": "bugs-orders",
            "label": "Find bugs in orders",
            "description": "Matched tracked Python source in orders with tests in orders/tests. Review scope before applying.",
            "source": "detected",
            "config": {
                "track": "bug",
                "editAllowlist": ["orders/**"],
                "focusedTestPaths": ["orders/tests"],
            },
        }
    ]
    assert any("PostgreSQL" in item for item in result["evidence"])
    assert any("Redis" in item for item in result["evidence"])
    assert any("benchmarks/jobs.py" in item for item in result["evidence"])
    assert any("does not measure runtime" in item for item in result["warnings"])


def test_ignores_uncommitted_and_untracked_configuration(repo):
    _write(repo, ".kirocrew/auto-improvement.json", "invalid json")
    _write(repo, "pyproject.toml", "malicious workspace changes")
    result = survey_repository(repo)
    assert result["declarationPath"] is None
    assert any("Django" in item for item in result["evidence"])


def test_reads_requested_local_revision(repo):
    earlier = _git(repo, "rev-parse", "HEAD")
    _declare(repo, {"version": 1, "workloads": [_perf()]})
    assert survey_repository(repo, earlier)["declarationPath"] is None
    assert survey_repository(repo)["workloads"][0]["source"] == "declaration"


def test_declared_perf_and_runner_are_reviewable_without_pin(repo):
    _write(repo, "scripts/run.sh", "#!/bin/sh\nexit 99\n")
    (repo / "scripts/run.sh").chmod(0o755)
    _declare(
        repo,
        {
            "version": 1,
            "environment": {"kind": "runner", "runnerPath": "scripts/run.sh"},
            "workloads": [_perf()],
        },
    )
    result = survey_repository(repo)
    assert result["environment"] == {
        "kind": "runner",
        "runnerPath": "scripts/run.sh",
        "pythonExecutable": "python",
    }
    assert result["workloads"][0]["config"]["benchmarkCommand"] == "python benchmarks/jobs.py"
    assert "runnerCommit" not in result["environment"]


@pytest.mark.parametrize(
    "field", ["benchmarkCommand", "benchmarkCanaryCommand", "benchmarkProtectedPaths"]
)
def test_rejects_incomplete_perf_workload(repo, field):
    workload = _perf()
    del workload["config"][field]
    _declare(repo, {"version": 1, "workloads": [workload]})
    with pytest.raises(ValueError):
        survey_repository(repo)


@pytest.mark.parametrize(
    "config",
    [
        {"unexpected": True},
        {"focusedTestPaths": ["../secret"]},
        {"focusedTestPaths": ["--override-ini=addopts="]},
        {"focusedTestPaths": ["orders/tests/test_models.py::test_ok"]},
        {"editAllowlist": ["/etc/**"]},
        {"editAllowlist": ["orders/../../**"]},
        {"benchmarkProtectedPaths": ["missing"]},
        {"suiteTimeoutSeconds": True},
        {"suiteTimeoutSeconds": float("inf")},
        {"focusedTestPaths": ["orders/link"]},
    ],
)
def test_rejects_invalid_workload_config(repo, config):
    _declare(repo, {"version": 1, "workloads": [{"id": "bad", "label": "Bad", "config": config}]})
    with pytest.raises(ValueError):
        survey_repository(repo)


@pytest.mark.parametrize(
    "environment",
    [
        {"kind": "runner", "runnerExecutable": "/bin/sh"},
        {"kind": "python", "pythonExecutable": "/usr/bin/python"},
        {"kind": "runner", "runnerPath": "../run.sh"},
        {"kind": "runner", "runnerPath": "orders/models.py", "runnerCommit": "a" * 40},
        {"kind": "runner", "runnerPath": "orders/models.py"},
        {"kind": "gateway", "variables": {"PATH": "/evil"}},
    ],
)
def test_rejects_unsafe_environment(repo, environment):
    _declare(repo, {"version": 1, "environment": environment})
    with pytest.raises(ValueError):
        survey_repository(repo)


@pytest.mark.parametrize(
    "body",
    ["x" * 65537, "[]", '{"version": true}', '{"version":1,"extra":2}', '{"version":'],
    ids=["oversized", "array", "boolean-version", "unknown-key", "invalid-json"],
)
def test_rejects_malformed_or_oversized_declaration(repo, body):
    _write(repo, ".kirocrew/auto-improvement.json", body)
    _commit(repo)
    with pytest.raises(ValueError):
        survey_repository(repo)


def test_refuses_tracked_symlink_declaration(repo):
    (repo / ".kirocrew").mkdir()
    (repo / ".kirocrew/auto-improvement.json").symlink_to("../pyproject.toml")
    _commit(repo)
    with pytest.raises(ValueError, match="symlink"):
        survey_repository(repo)


def test_refuses_symlinks_inside_declared_paths(repo):
    (repo / "orders/link").symlink_to("/etc/passwd")
    _declare(repo, {"version": 1, "workloads": [_perf()]})
    with pytest.raises(ValueError, match="symlink"):
        survey_repository(repo)


def test_rejects_duplicate_workload_ids(repo):
    _declare(repo, {"version": 1, "workloads": [_perf(), _perf()]})
    with pytest.raises(ValueError, match="unique"):
        survey_repository(repo)


def test_rejects_duplicate_json_keys(repo):
    _write(repo, ".kirocrew/auto-improvement.json", '{"version":1,"version":1}')
    _commit(repo)
    with pytest.raises(ValueError, match="Duplicate"):
        survey_repository(repo)


def test_rejects_option_shaped_ref(repo):
    with pytest.raises(ValueError, match="local ref"):
        survey_repository(repo, "--help")


def test_detects_centralized_nested_test_directories(repo):
    _write(repo, "data_import/services.py", "pass\n")
    _write(repo, "tests/unit/data_import/test_services.py", "pass\n")
    _write(repo, "tests/unit/data_import/conftest.py", "pass\n")
    _commit(repo)
    result = survey_repository(repo)
    workload = next(item for item in result["workloads"] if item["id"] == "bugs-data_import")
    assert workload["config"]["focusedTestPaths"] == ["tests/unit/data_import"]
    assert all(item["id"] != "bugs-tests" for item in result["workloads"])


def test_import_mapping_offers_reviewable_exact_file_scope_and_unmapped_inventory(repo):
    _write(repo, "conversion.py", "pass\n")
    _write(repo, "unmapped/service.py", "pass\n")
    _write(repo, "tests/test_result.py", "import conversion\n")
    _commit(repo)
    result = survey_repository(repo)
    workload = next(item for item in result["workloads"] if item["id"] == "bugs-conversion.py")
    assert workload["config"]["editAllowlist"] == ["conversion.py"]
    assert workload["config"]["focusedTestPaths"] == ["tests/test_result.py"]
    assert any(
        a["path"] == "unmapped" and a["mappingConfidence"] == "unmapped" for a in result["areas"]
    )


def test_named_test_directory_retains_cross_module_import_mappings(repo):
    _write(repo, "tests/test_checkout.py", "from orders.models import Order\n")
    _commit(repo)
    result = survey_repository(repo)
    workloads = [item for item in result["workloads"] if item["id"] == "bugs-orders"]
    assert len(workloads) == 1
    assert workloads[0]["config"]["focusedTestPaths"] == ["orders/tests", "tests/test_checkout.py"]


def test_oversized_uncompactable_mapping_never_becomes_partial_workload(repo):
    for index in range(101):
        _write(repo, f"tests/test_{index}.py", "import orders.models\n")
    _commit(repo)
    result = survey_repository(repo)
    assert not any(item["id"] == "bugs-orders" for item in result["workloads"])
    area = next(item for item in result["areas"] if item["path"] == "orders")
    assert area["testPathsTruncated"] is True
    assert any("no automatic workload" in line for line in area["evidence"])


def test_detected_mapping_containing_symlink_is_visible_but_not_executable(repo):
    (repo / "orders/tests/link").symlink_to("/etc/passwd")
    _commit(repo)
    result = survey_repository(repo)
    assert any(item["path"] == "orders" for item in result["areas"])
    assert not any(item["id"] == "bugs-orders" for item in result["workloads"])


@pytest.mark.parametrize(
    "branch", ["feature", "origin/feature", "feature/nested", "origin/feature/nested"]
)
def test_survey_uses_remote_before_checkout_then_refreshed_local_branch(repo, branch):
    bare = branch.removeprefix("origin/")
    old = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/" + bare, old)
    assert resolve_survey_ref(repo, branch) == "refs/remotes/origin/" + bare
    assert survey_repository(repo, resolve_survey_ref(repo, branch))["revision"] == old
    _write(repo, "orders/new.py", "pass\n")
    new = _commit(repo)
    _git(repo, "update-ref", "refs/heads/" + bare, new)
    assert resolve_survey_ref(repo, branch) == "refs/heads/" + bare
    assert survey_repository(repo, resolve_survey_ref(repo, branch))["revision"] == new
    assert _git(repo, "rev-parse", "refs/remotes/origin/" + bare) == old


@pytest.mark.parametrize("ref", ["HEAD", "refs/heads/main", "refs/remotes/origin/feature"])
def test_survey_preserves_explicit_ref(repo, ref):
    assert resolve_survey_ref(repo, ref) == ref


def test_survey_ref_resolution_surfaces_git_failure(repo, monkeypatch):
    from kiro_crew.apps.builtins.auto_improvement.backend import repository_survey

    monkeypatch.setattr(
        repository_survey, "_run_git", lambda *args: subprocess.CompletedProcess([], 128)
    )
    with pytest.raises(ValueError, match="resolve"):
        resolve_survey_ref(repo, "feature")
