"""Validation of repository measurement settings."""

import math
import sys
from pathlib import Path

from .benchmark_result import normalize_benchmark_protected_paths
from .environment import normalize_test_environment

MAX_SUITE_TIMEOUT_SECONDS = 86400


def normalize_suite_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value <= MAX_SUITE_TIMEOUT_SECONDS
        or not math.isfinite(value)
    ):
        raise ValueError("suiteTimeoutSeconds must be positive and finite, at most 86400")
    return float(value)


def normalize_measurement_config(config: dict) -> dict:
    paths = config.get("focusedTestPaths", [])
    if not isinstance(paths, list) or any(
        not isinstance(path, str)
        or not path
        or path.startswith("-")
        or Path(path).is_absolute()
        or ".." in Path(path).parts
        or any(char in path for char in "\\:*?[]\n\r\x00")
        for path in paths
    ):
        raise ValueError(
            "focusedTestPaths must contain explicit repository-relative pytest paths, not options, globs or node IDs"
        )
    if paths:
        variables = normalize_test_environment(config.get("testEnvironment")).get("variables", {})
        if any(key.upper() == "PYTEST_ADDOPTS" and value for key, value in variables.items()):
            raise ValueError(
                "focusedTestPaths requires empty PYTEST_ADDOPTS; remove it from test-environment variables so full regression cannot be narrowed"
            )
    timeout = config.get("fullRegressionTimeoutSeconds", 900)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= sys.float_info.max
    ):
        raise ValueError("fullRegressionTimeoutSeconds must be positive and finite")
    track = config.get("track", "bug")
    if track not in ("bug", "perf"):
        raise ValueError("track must be bug or perf")
    mode = config.get("benchmarkResultMode", "wall")
    if mode not in ("wall", "structured"):
        raise ValueError("benchmarkResultMode must be wall or structured")
    commands = {}
    for key in ("benchmarkCommand", "benchmarkCanaryCommand"):
        command = config.get(key, "")
        if not isinstance(command, str) or any(ord(c) < 32 for c in command):
            raise ValueError(f"{key} must be a single-line command")
        commands[key] = command.strip()
    if not commands["benchmarkCommand"] and (commands["benchmarkCanaryCommand"] or mode != "wall"):
        raise ValueError("custom benchmark options require benchmarkCommand")
    return dict(
        commands,
        track=track,
        benchmarkResultMode=mode,
        benchmarkProtectedPaths=normalize_benchmark_protected_paths(
            config.get("benchmarkProtectedPaths", [])
        ),
        focusedTestPaths=list(paths),
        fullRegressionTimeoutSeconds=float(timeout),
        suiteTimeoutSeconds=normalize_suite_timeout(config.get("suiteTimeoutSeconds", 900)),
    )
