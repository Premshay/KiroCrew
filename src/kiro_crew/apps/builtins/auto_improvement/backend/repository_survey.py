"""Read committed repository evidence and validate operator-reviewable workloads.

Surveying never imports repository modules, runs declared commands, fetches objects,
or changes saved configuration. Suggestions are not execution approval.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..profiles.github_repo.environment import normalize_test_environment
from ..profiles.github_repo.measurement_settings import normalize_measurement_config
from .repository_inventory import repository_inventory
from .runner_source import _run_git

_DECLARATION = ".kirocrew/auto-improvement.json"
_MAX_BLOB = 64 * 1024
_MAX_FILES = 100_000
_CONFIG_KEYS = {
    "track",
    "focusedTestPaths",
    "editAllowlist",
    "benchmarkCommand",
    "benchmarkCanaryCommand",
    "benchmarkProtectedPaths",
    "benchmarkResultMode",
    "suiteTimeoutSeconds",
    "fullRegressionTimeoutSeconds",
}


def _git(root: Path, *args: str) -> str:
    result = _run_git(list(args), root)
    if result.returncode:
        raise ValueError("Repository survey could not read the committed repository")
    return result.stdout


def resolve_survey_ref(root: Path, branch: str) -> str:
    """Prefer the local branch refreshed by checkout; use origin before first checkout."""
    if (
        not isinstance(branch, str)
        or not branch
        or branch.startswith("-")
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in branch)
    ):
        raise ValueError("Repository survey requires a valid local ref")
    if branch == "HEAD" or branch.startswith("refs/"):
        return branch
    bare = branch.removeprefix("origin/")
    local = "refs/heads/" + bare
    result = _run_git(["show-ref", "--verify", "--quiet", local], Path(root))
    if result.returncode == 0:
        return local
    if result.returncode == 1:
        return "refs/remotes/origin/" + bare
    raise ValueError("Repository survey could not resolve the selected branch")


def _entries(root: Path, revision: str) -> dict[str, tuple[str, str, int]]:
    entries = {}
    for row in _git(root, "ls-tree", "-r", "-l", "-z", revision).split("\0"):
        if not row:
            continue
        metadata, path = row.split("\t", 1)
        mode, kind, oid, size = metadata.split()
        if kind == "blob":
            entries[path] = (mode, oid, int(size))
        if len(entries) > _MAX_FILES:
            raise ValueError("Repository survey exceeds the 100,000 tracked-file limit")
    return entries


def _blob(root: Path, entries: dict, path: str) -> str:
    mode, oid, size = entries[path]
    if mode not in {"100644", "100755"}:
        raise ValueError(f"Repository survey refuses symlink: {path}")
    if size > _MAX_BLOB:
        raise ValueError(f"Repository survey file exceeds 64 KiB: {path}")
    return _git(root, "cat-file", "blob", oid)


def _path(value: object, entries: dict, *, glob: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Workload paths must be nonempty repository-relative paths")
    base = value[:-3] if glob and value.endswith("/**") else value
    if (
        base.startswith("-")
        or any(c in base for c in "\\:*?[]")
        or any(ord(c) < 32 or ord(c) == 127 for c in base)
        or any(p in {"", ".", "..", ".git"} for p in base.lower().split("/"))
    ):
        raise ValueError(f"Unsafe workload path: {value}")
    matches = [p for p in entries if p == base or p.startswith(base + "/")]
    if not matches:
        raise ValueError(f"Workload path is not tracked: {value}")
    if any(entries[p][0] not in {"100644", "100755"} for p in matches):
        raise ValueError(f"Workload path contains a symlink: {value}")
    return value


def _text(value: object, field: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"Invalid workload {field}")
    return value.strip()


def _workload(value: object, entries: dict) -> dict:
    if not isinstance(value, dict) or value.keys() - {"id", "label", "description", "config"}:
        raise ValueError("Workloads must contain only id, label, description and config")
    identifier = _text(value.get("id"), "id", 64)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", identifier):
        raise ValueError("Workload id must use letters, numbers, underscores or hyphens")
    config = value.get("config")
    if not isinstance(config, dict) or config.keys() - _CONFIG_KEYS:
        raise ValueError("Workload config contains unsupported settings")
    normalized = normalize_measurement_config(config)
    for key in ("focusedTestPaths", "benchmarkProtectedPaths", "editAllowlist"):
        paths = config.get(key, [])
        if not isinstance(paths, list) or len(paths) > 100:
            raise ValueError(f"{key} must be a list of at most 100 paths")
        for path in paths:
            _path(path, entries, glob=key == "editAllowlist")
    if "suiteTimeoutSeconds" in config:
        timeout = config["suiteTimeoutSeconds"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 86400
        ):
            raise ValueError("suiteTimeoutSeconds must be between 0 and 86400 seconds")
    if normalized["track"] == "perf" and not all(
        normalized[key]
        for key in ("benchmarkCommand", "benchmarkCanaryCommand", "benchmarkProtectedPaths")
    ):
        raise ValueError(
            "Performance workloads require a benchmark, slowed control and protected paths"
        )
    return {
        "id": identifier,
        "label": _text(value.get("label"), "label", 120),
        "description": _text(value.get("description", value.get("label")), "description", 1000),
        "source": "declaration",
        "config": {key: normalized.get(key, item) for key, item in config.items()},
    }


def _declaration(root: Path, entries: dict) -> tuple[dict | None, list[dict]]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"Duplicate repository declaration field: {key}")
            result[key] = item
        return result

    try:
        value = json.loads(_blob(root, entries, _DECLARATION), object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid repository declaration JSON") from exc
    if (
        not isinstance(value, dict)
        or value.keys() - {"version", "environment", "workloads"}
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("Repository declaration requires version 1 and supported fields")
    environment = None
    if "environment" in value:
        raw = value["environment"]
        if not isinstance(raw, dict) or any(k in raw for k in ("runnerExecutable", "runnerCommit")):
            raise ValueError(
                "Repository environments must not supply host executables or runner pins"
            )
        if raw.get("kind") == "python" or Path(str(raw.get("pythonExecutable", ""))).is_absolute():
            raise ValueError("Repository environments must not supply host Python paths")
        environment = normalize_test_environment(raw)
        if environment["kind"] == "runner":
            _path(environment["runnerPath"], entries)
            if entries.get(environment["runnerPath"], (None,))[0] != "100755":
                raise ValueError("Declared runnerPath must be a tracked executable file")
    raw_workloads = value.get("workloads", [])
    if not isinstance(raw_workloads, list) or len(raw_workloads) > 40:
        raise ValueError("Declaration requires at most 40 workloads")
    workloads = [_workload(item, entries) for item in raw_workloads]
    if len({item["id"] for item in workloads}) != len(workloads):
        raise ValueError("Workload ids must be unique")
    return environment, workloads


def survey_repository(root: Path, ref: str = "HEAD") -> dict:
    """Read a local ref; malformed declarations fail without partial application."""
    root = Path(root)
    if not isinstance(ref, str) or not ref or ref.startswith("-") or any(ord(c) < 32 for c in ref):
        raise ValueError("Repository survey requires a valid local ref")
    revision = _git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("Repository survey requires a committed revision")
    entries = _entries(root, revision)
    inventory = repository_inventory(entries, lambda path: _blob(root, entries, path))
    evidence = []
    warnings = [
        "Test collection does not measure runtime. Check the environment and calibrate performance workloads before running.",
        "Suggestions use the selected local Git revision; they exclude uncommitted changes and are not a complete coverage map.",
    ]
    for path in (
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "manage.py",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ):
        if path not in entries or entries[path][0] not in {"100644", "100755"}:
            continue
        evidence.append(f"Tracked configuration: {path}")
        if entries[path][2] <= _MAX_BLOB:
            content = _blob(root, entries, path).lower()
            for marker, label in (
                ("django", "Django"),
                ("pytest", "pytest"),
                ("postgres", "PostgreSQL"),
                ("redis", "Redis"),
            ):
                if marker in content:
                    evidence.append(
                        f"{label} reference in {path}; execution requirements need verification"
                    )
    perf = sorted(
        p
        for p in entries
        if entries[p][0] in {"100644", "100755"}
        and any(token in p.lower() for token in ("benchmark", "test_perf", "performance"))
    )
    evidence.extend(f"Possible performance workload: {p}" for p in perf[:20])
    if perf:
        warnings.append(
            "Performance files were found; no benchmark or slowed control command was inferred from filenames."
        )
    environment, declared = _declaration(root, entries) if _DECLARATION in entries else (None, [])
    if declared or environment:
        warnings.append(
            "Repository declarations are untrusted suggestions. Review commands and environment before saving; survey executes nothing."
        )
    ids = {item["id"] for item in declared}
    workloads = list(declared)
    for area in inventory["areas"]:
        identifier = "bugs-" + area["id"]
        focused = [
            path for path in area["testPaths"] if path.endswith(".py") or path not in entries
        ]
        if (
            identifier in ids
            or not area["pythonSupported"]
            or not focused
            or area["testPathsTruncated"]
        ):
            continue
        try:
            _path(area["path"], entries)
            for path in focused:
                _path(path, entries)
        except ValueError:
            area["evidence"].append(
                "Source or test mapping contains unsafe paths; no automatic workload is offered"
            )
            continue
        workloads.append(
            {
                "id": identifier,
                "label": f"Find bugs in {area['label']}",
                "description": f"Matched tracked Python source in {area['path']} with tests in {', '.join(focused)}. Review scope before applying.",
                "source": "detected",
                "config": {
                    "track": "bug",
                    "editAllowlist": [
                        area["path"] if area["path"] in entries else area["path"] + "/**"
                    ],
                    "focusedTestPaths": focused,
                },
            }
        )
    return {
        **inventory,
        "revision": revision,
        "declarationPath": _DECLARATION if _DECLARATION in entries else None,
        "evidence": evidence,
        "warnings": warnings,
        "workloads": workloads,
        "environment": environment,
    }
