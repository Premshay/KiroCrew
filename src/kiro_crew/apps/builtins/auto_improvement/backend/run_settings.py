"""Validated operator scope and budgets, remembered separately from the ruler."""

from __future__ import annotations

import math

WRITABLE_CONFIG_KEYS = frozenset(
    {
        "acceptUnsandboxedAgentRisk",
        "autoDraftPr",
        "autoPublish",
        "bandCapMs",
        "benchmarkCanaryCommand",
        "benchmarkCommand",
        "benchmarkProtectedPaths",
        "benchmarkResultMode",
        "branch",
        "calibrationReps",
        "canaryAdvisory",
        "directCommit",
        "editAllowlist",
        "focusedTestPaths",
        "forceBugSeeds",
        "fullRegressionTimeoutSeconds",
        "maxCostUsd",
        "maxCycles",
        "maxHours",
        "measureReps",
        "profile",
        "proposerDeep",
        "proposerWide",
        "quiesceAfter",
        "reproduceReps",
        "scopeDiffBase",
        "suiteTimeoutSeconds",
        "testEnvironment",
        "track",
        "watcherAcceptEgressRisk",
        "watcherAutoStart",
    }
)


def normalize_run_settings(config: dict) -> dict:
    from .runner import DEFAULT_MAX_COST_USD, DEFAULT_MAX_CYCLES, DEFAULT_MAX_HOURS

    result: dict[str, object] = {}
    for key, default, low, high, integer in (
        ("maxCycles", DEFAULT_MAX_CYCLES, 1, 1000, True),
        ("maxHours", DEFAULT_MAX_HOURS, 0.01, 168, False),
        ("maxCostUsd", DEFAULT_MAX_COST_USD, 0.01, 100000, False),
        ("calibrationReps", 5, 2, 10, True),
        ("measureReps", None, 2, 100, True),
    ):
        value = config.get(key, default)
        if key == "measureReps" and value is None:
            result[key] = None
            continue
        if (
            type(value) not in (int, float)
            or not low <= value <= high
            or not math.isfinite(value)
            or (integer and int(value) != value)
        ):
            raise ValueError(
                f"{key} must be {'an integer' if integer else 'a number'} from {low} to {high}"
            )
        result[key] = int(value) if integer else float(value)

    paths = config.get("editAllowlist", [])
    if (
        not isinstance(paths, list)
        or len(paths) > 100
        or any(
            not isinstance(path, str)
            or not path
            or len(path) > 500
            or path != path.strip()
            or path.startswith(("/", "-"))
            or any(part in ("", ".", "..", ".git") for part in path.lower().split("/"))
            or any(char in path for char in "\\:")
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            for path in paths
        )
    ):
        raise ValueError(
            "editAllowlist must contain at most 100 repository-relative path globs without traversal"
        )
    result["editAllowlist"] = list(dict.fromkeys(paths))
    ref = config.get("scopeDiffBase", "")
    if (
        not isinstance(ref, str)
        or len(ref) > 250
        or ref.startswith("-")
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in ref)
    ):
        raise ValueError("scopeDiffBase must be a short Git ref without whitespace or options")
    result["scopeDiffBase"] = ref
    return result
