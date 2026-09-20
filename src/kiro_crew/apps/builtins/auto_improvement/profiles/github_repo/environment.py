from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from kiro_crew.platform.app_execution import current_app_execution
from kiro_crew.platform.context import current_context
from kiro_crew.platform.governance import Decision, OrdinalControl, resolve_ordinal
from kiro_crew.platform.governance_profiles import governance_permits, resolve_active_scope

from ...spine.git_safety import _resolve_gitdir
from ...spine.push_policy import strip_credential_env

__all__ = ["RunnerAdmissionError", "TestEnvironment", "normalize_test_environment"]

_KEYS = {"kind", "pythonExecutable", "runnerExecutable", "variables"}
_RUNNER_CLEANUP_SECONDS = 30


class RunnerAdmissionError(RuntimeError):
    pass


def _text(value: object) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("test environment values must be strings without control characters")
    if any(c in value for c in ";|&`$<>"):
        raise ValueError("shell expressions are not supported in test environments")
    return value


def _variables(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("variables must be a string-to-string mapping")
    result = {}
    for key, val in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("invalid environment variable name")
        if not strip_credential_env({key: ""}) or any(
            marker in key.upper() for marker in ("PASSWORD", "PASSWD", "AUTH", "PRIVATE_KEY")
        ):
            raise ValueError(f"credential-shaped environment variable is forbidden: {key}")
        if key.upper() in {
            "HOME",
            "PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "TMPDIR",
            "DOCKER_HOST",
            "DOCKER_CONFIG",
        } or key.upper().startswith(("LD_", "DYLD_", "KIROCREW_RUNNER_")):
            raise ValueError(f"reserved environment variable: {key}")
        result[key] = _text(val)
    return result


def normalize_test_environment(value: object) -> dict:
    if value is None:
        return {"kind": "gateway"}
    if isinstance(value, dict) and value.get("kind") == "container":
        raise ValueError("container environments are unsupported; select a repository runner")
    if not isinstance(value, dict) or value.keys() - _KEYS:
        raise ValueError("testEnvironment must be an object with supported keys")
    kind = value.get("kind", "gateway")
    if not isinstance(kind, str) or kind not in {"gateway", "python", "runner"}:
        raise ValueError("test environment kind must be gateway, python, or runner")
    out: dict = {"kind": kind}
    if kind != "runner" and "runnerExecutable" in value:
        raise ValueError("runnerExecutable applies only to runner environments")
    if kind == "gateway" and "pythonExecutable" in value:
        raise ValueError("select python kind to configure an interpreter")
    if kind in {"python", "runner"}:
        executable = _text(value.get("pythonExecutable", "python" if kind == "runner" else ""))
        path = Path(executable)
        if not executable or executable.startswith("-") or ".." in path.parts:
            raise ValueError("pythonExecutable must be a single executable, not a shell command")
        if kind == "python" and not path.is_absolute():
            raise ValueError("pythonExecutable must be an absolute lexical path")
        if (
            kind == "runner"
            and not path.is_absolute()
            and not re.fullmatch(r"[\w.+-]+", executable)
        ):
            raise ValueError("runner Python must be an executable name or an absolute path")
        out["pythonExecutable"] = executable
    if kind == "runner":
        executable = _text(value.get("runnerExecutable", ""))
        if not Path(executable).is_absolute() or ".." in Path(executable).parts:
            raise ValueError("runnerExecutable must be an absolute path")
        out["runnerExecutable"] = executable
    if "variables" in value:
        out["variables"] = _variables(value["variables"])
    return out


class TestEnvironment:
    __test__ = False

    def __init__(self, config: dict | None, clone_path: Path, sandbox_run: Callable):
        self._config = normalize_test_environment(config)
        self._clone = Path(clone_path)
        self._sandbox_run = sandbox_run
        self._runner_identity: dict | None = None
        if self._config["kind"] == "runner":
            self._runner_identity = self._identify_runner()

    @property
    def identity(self) -> dict:
        result = copy.deepcopy(self._config)
        result["pythonExecutable"] = self.python_argv()[0]
        if self._runner_identity is not None:
            result["runnerIdentity"] = dict(self._runner_identity)
        return result

    def python_argv(self, *args: str) -> list[str]:
        return [self._config.get("pythonExecutable", sys.executable), *args]

    def _source(self, cwd: Path) -> Path:
        clone, source = self._clone, Path(cwd)
        for path in (clone, source):
            if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
                raise ValueError("clone and cwd must be absolute paths without symlink aliases")
            if (
                len(path.parts) < 4
                or path == Path.home()
                or any(c in _text(str(path)) for c in ',"')
            ):
                raise ValueError("refusing a host-root or ambiguous source")
            if not path.is_dir():
                raise ValueError("runner source must be a directory")
        if source != clone:
            if source.parent != clone.parent and source.parent != clone.parent / "worktrees":
                raise ValueError("cwd must be the base clone or a sibling managed worktree")
            if _resolve_gitdir(source) != clone / ".git":
                raise ValueError("candidate must be a linked worktree of the configured clone")
        if _resolve_gitdir(clone) != clone / ".git":
            raise ValueError("base clone must have its own git directory")
        return source

    def run(
        self, argv: list[str], *, cwd: Path, timeout: float, env: dict | None = None
    ) -> subprocess.CompletedProcess[str]:
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
        ):
            raise ValueError("argv must be a nonempty argument vector")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        merged = dict(env or {})
        merged.update(self._config.get("variables", {}))
        if self._config["kind"] != "runner":
            return self._sandbox_run(list(argv), cwd=cwd, timeout=timeout, env=merged or None)
        source = self._source(cwd)
        if argv[0] != self.python_argv()[0]:
            raise ValueError("runner commands must use the configured Python executable")
        command = [self._config["runnerExecutable"], "--source", str(source), "--", *argv]
        # A Python runner must not import its control code from the candidate's
        # PYTHONPATH. The recipe sets target import paths after selecting its source.
        merged["PYTHONPATH"] = ""
        self._admit(command, source)
        self._validate_runner()
        # Daemon-owned workloads outlive a killed launcher. Recipes must stop work
        # at this deadline and finish teardown before the outer executor kills them.
        merged["KIROCREW_RUNNER_DEADLINE"] = str(time.time() + timeout)
        merged["KIROCREW_RUNNER_CLEANUP_SECONDS"] = str(_RUNNER_CLEANUP_SECONDS)
        try:
            return self._sandbox_run(
                command, cwd=source, timeout=timeout + _RUNNER_CLEANUP_SECONDS, env=merged
            )
        finally:
            self._validate_runner()

    def _identify_runner(self) -> dict:
        runner = Path(self._config["runnerExecutable"])
        if runner.resolve(strict=True) != runner or not runner.is_file():
            raise RunnerAdmissionError("Runner must be a regular file without symlink aliases")
        # All candidate checkouts share this managed scratch tree. A repository
        # cannot supply the program that decides how its own measurements execute.
        if runner.is_relative_to(self._clone.resolve().parent):
            raise RunnerAdmissionError("Runner must be outside the managed checkout tree")
        if not os.access(runner, os.X_OK) or runner.stat().st_nlink != 1:
            raise RunnerAdmissionError("Runner must be executable and not hard-linked")
        info = runner.stat()
        return {
            "sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
            "device": info.st_dev,
            "inode": info.st_ino,
        }

    def _validate_runner(self) -> None:
        if self._identify_runner() != self._runner_identity:
            raise RunnerAdmissionError(
                "Runner identity changed; restore it before starting a new run"
            )

    def _admit(self, command: list[str], source: Path) -> None:
        from kiro_crew.sel import sel

        from ...backend import store

        execution = current_app_execution()
        if execution is None or execution.app != store.APP_NAME or not execution.user.strip():
            raise RunnerAdmissionError("Repository runner requires authenticated app execution")
        config = store.read_json(store.config_path(), {})
        if config.get("clone") != str(self._clone):
            raise RunnerAdmissionError("Source is not the authenticated app's configured clone")
        ctx = current_context()
        profile = resolve_active_scope(execution.session_key, app=execution.app)
        floor = resolve_ordinal(ctx.governance, profile, "sandbox.min_level")
        strict = OrdinalControl("sandbox", "strict")
        if floor is not None and strict.compose(floor).value != "strict":
            raise RunnerAdmissionError(
                "Repository runner cannot enforce the effective sandbox floor"
            )
        for scope, item in (
            ("apps", execution.app),
            ("commands", shlex.join(command)),
            ("commands", shlex.join(command[4:])),
            ("filesystem.read", str(source)),
            ("filesystem.read", command[0]),
        ):
            decision = governance_permits(
                scope, item, session_key=execution.session_key, app=execution.app, fail_closed=True
            )
            if not isinstance(decision, Decision):
                raise RunnerAdmissionError("Runner admission returned an invalid decision")
            try:
                sel().log_governance_decision(
                    session_key=execution.session_key,
                    tool_name="repository_runner",
                    scope=scope,
                    item=item,
                    outcome="allowed" if decision.permitted else "denied",
                    reason=decision.reason,
                    rule=decision.rule,
                    layer=decision.layer,
                    critical=True,
                )
            except Exception as exc:
                raise RunnerAdmissionError(
                    "Runner audit unavailable; restore SEL persistence"
                ) from exc
            if not decision.permitted:
                raise RunnerAdmissionError(
                    f"Repository runner denied by {scope}: {decision.reason}"
                )
        # A launcher can delegate to a daemon outside its own OS sandbox. Root
        # path admission cannot enforce descendant or destination restrictions there.
        for policy in (ctx.governance, profile):
            for scope in ("filesystem.read", "filesystem.write", "network.egress"):
                if policy is not None and policy.get(scope) is not None:
                    raise RunnerAdmissionError(
                        f"Repository runner cannot enforce delegated {scope} rules; "
                        "use an execution environment governed at the workload boundary"
                    )
