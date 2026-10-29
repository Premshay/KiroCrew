"""App-managed repository runners: a pinned commit, extracted outside every checkout.

A runner environment may name its recipe by ``runnerPath`` inside the target repository
instead of an absolute ``runnerExecutable``. The app then fetches the configured branch into
its own bare mirror, pins the commit it resolved, and extracts that commit into a read-only
snapshot under ``store.runner_snapshot_path()``, outside the crew data home that the
strict sandbox masks. Nothing outside the app owns these files, so no worktree cleanup
can remove the runner, and a missing snapshot is rebuilt from the pin.

The pin moves only when the test environment is saved without a commit. A runner that
followed the branch would let a merged change alter how measurements execute between two
samples the ruler compares.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from kiro_crew.platform.context import redact_via_context
from kiro_crew.subprocess_utf8 import UTF8_TEXT

from . import store

__all__ = [
    "is_commit",
    "managed_runner_executable",
    "prepare_runner",
    "prune_snapshots",
]

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_PIN_REF = "refs/auto-improvement/runner-pin"
_FETCH_TIMEOUT_S = 300


def is_commit(value: str) -> bool:
    return bool(_COMMIT_RE.fullmatch(value))


def _runners_root() -> Path:
    return store.data_dir() / "runners"


def _snapshots_root() -> Path:
    return store.runner_snapshot_path()


def _snapshot_dir(commit: str) -> Path:
    return _snapshots_root() / commit


def managed_runner_executable(environment: dict) -> Path:
    """Where a pinned runner lives. The path only; :func:`prepare_runner` creates it."""
    return _snapshot_dir(environment["runnerCommit"]) / environment["runnerPath"]


def _mirror(config: dict) -> Path:
    path = _runners_root() / "mirrors" / f"{store.repository_key(config)[:24]}.git"
    if not (path / "HEAD").is_file():
        path.mkdir(parents=True, exist_ok=True)
        _git(path, "init", "--quiet", "--bare", check=True)
    return path


def _git(
    mirror: Path,
    *args: str,
    network_protocol: str = "",
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess:
    from .clone_setup import _GIT_SAFE_CONFIG, _git_env

    proc = subprocess.run(
        ["git", f"--git-dir={mirror}", *_GIT_SAFE_CONFIG, *args],
        capture_output=True,
        timeout=_FETCH_TIMEOUT_S,
        shell=False,
        env=_git_env(network_protocol=network_protocol),
        cwd=str(mirror),
        **(UTF8_TEXT if text else {}),
    )
    if check and proc.returncode:
        raise RuntimeError(f"git {args[0]} failed in the runner mirror")
    return proc


def _fetch(config: dict, mirror: Path, refspec: str) -> None:
    from ..profiles.github_repo.pr_recipe import _prefer_authenticated_remote
    from .clone_setup import resolve_origin_url

    remote = resolve_origin_url(config)
    if not remote:
        raise ValueError("no validated repository URL configured — re-run repository setup")
    remote = _prefer_authenticated_remote(remote)
    protocol = "ssh" if remote.startswith("git@") else (urlparse(remote).scheme or "file")
    fetched = _git(
        mirror,
        "-c",
        "credential.helper=!gh auth git-credential",
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-auto-maintenance",
        "--no-write-commit-graph",
        remote,
        refspec,
        network_protocol=protocol,
    )
    if fetched.returncode:
        detail = redact_via_context(fetched.stderr or "").strip().splitlines()[-1:] or [""]
        raise ValueError(f"could not fetch the runner source: {detail[0][:160]}")


def _pin(config: dict, mirror: Path) -> str:
    from .clone_setup import is_valid_branch_name

    branch = str(config.get("branch") or "main").strip()
    bare = branch.split("/", 1)[1] if branch.startswith("origin/") else branch
    if not is_valid_branch_name(bare):
        raise ValueError(f"invalid branch name: {branch!r}")
    _fetch(config, mirror, f"+refs/heads/{bare}:{_PIN_REF}")
    resolved = _git(mirror, "rev-parse", "--verify", f"{_PIN_REF}^{{commit}}")
    commit = resolved.stdout.strip()
    if resolved.returncode or not is_commit(commit):
        raise ValueError(f"could not resolve {bare} in the runner mirror")
    return commit


def _make_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = Path(dirpath, name)
            if not path.is_symlink():
                mode = path.stat().st_mode
                path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        for name in dirnames:
            path = Path(dirpath, name)
            if not path.is_symlink():
                path.chmod(0o555)
    root.chmod(0o555)


def _make_writable(root: Path) -> None:
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            path = Path(dirpath, name)
            if not path.is_symlink():
                path.chmod(0o755)
    root.chmod(0o755)


def _remove(root: Path) -> None:
    if root.exists():
        _make_writable(root)
        shutil.rmtree(root)


def _extract(config: dict, mirror: Path, commit: str) -> None:
    present = _git(mirror, "cat-file", "-e", f"{commit}^{{commit}}")
    if present.returncode:
        _fetch(config, mirror, commit)
    archive = _git(mirror, "archive", "--format=tar", commit, text=False)
    if archive.returncode:
        raise ValueError(f"could not read runner commit {commit[:12]} from the mirror")
    target = _snapshot_dir(commit)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{commit[:12]}-", dir=target.parent))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
            tar.extractall(staging, filter="data")
        _make_read_only(staging)
        staging.rename(target)
    except BaseException:
        _remove(staging)
        raise


def prepare_runner(config: dict, *, pin: bool = False) -> str:
    """Make the configured runner executable present; returns a note, or ``""`` if unmanaged.

    With ``pin``, an environment without ``runnerCommit`` is pinned to the branch's
    current commit, written into ``config``. Without it, an unpinned runner is refused so a
    run never silently follows the branch. Callers hold the clone lock.
    """
    from ..profiles.github_repo.environment import RunnerAdmissionError, normalize_test_environment

    environment = normalize_test_environment(config.get("testEnvironment"))
    if environment["kind"] != "runner" or "runnerPath" not in environment:
        return ""
    commit = environment.get("runnerCommit", "")
    mirror = _mirror(config)
    action = "using"
    if not commit:
        if not pin:
            raise RunnerAdmissionError(
                "The runner commit is not pinned; save the test environment to pin it"
            )
        commit = _pin(config, mirror)
        environment["runnerCommit"] = commit
        config["testEnvironment"] = environment
        action = "pinned"
    if not _snapshot_dir(commit).is_dir():
        _extract(config, mirror, commit)
        if action == "using":
            action = "rebuilt"
    executable = managed_runner_executable(environment)
    if not executable.is_file() or executable.is_symlink():
        raise RunnerAdmissionError(
            f"{environment['runnerPath']} is not a regular file at runner commit {commit[:12]}"
        )
    return f"{action} runner {environment['runnerPath']} @ {commit[:12]}"


def _referenced_commits(config: dict) -> set[str]:
    environments = [config.get("testEnvironment")]
    environments += list((config.get("repositoryTestEnvironments") or {}).values())
    return {
        env["runnerCommit"]
        for env in environments
        if isinstance(env, dict) and is_commit(str(env.get("runnerCommit") or ""))
    }


def prune_snapshots(config: dict) -> None:
    """Remove snapshots no saved environment references, including interrupted extractions."""
    root = _snapshots_root()
    if not root.is_dir():
        return
    keep = _referenced_commits(config)
    for entry in root.iterdir():
        if entry.name not in keep:
            _remove(entry)
