from __future__ import annotations

import json
import math
import shlex
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

PREFIX = "AUTO_IMPROVEMENT_METRIC"


@dataclass(frozen=True)
class BenchmarkResult:
    identity: tuple[str, str, str]
    value: float
    outer_seconds: float


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate benchmark JSON key")
        result[key] = value
    return result


def parse_benchmark_result(
    proc: CompletedProcess[str], outer_seconds: float, mode: str
) -> BenchmarkResult:
    if proc.returncode != 0:
        raise ValueError("benchmark command failed; fix the workload before measuring")
    if not math.isfinite(outer_seconds) or outer_seconds <= 0:
        raise ValueError("invalid outer benchmark duration")
    if mode == "wall":
        return BenchmarkResult(
            ("benchmark_wall_seconds", "seconds", "configured-command"),
            outer_seconds,
            outer_seconds,
        )
    if mode != "structured":
        raise ValueError("benchmarkResultMode must be wall or structured")
    markers = [line for line in (proc.stdout or "").splitlines() if PREFIX in line]
    if len(markers) != 1 or not markers[0].startswith(PREFIX):
        raise ValueError("benchmark requires exactly one stdout metric line")
    data = json.loads(markers[0][len(PREFIX) :], object_pairs_hook=_object)
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "metric",
        "unit",
        "value",
        "workload_id",
    }:
        raise ValueError("invalid benchmark metric schema")
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["unit"] != "seconds"
    ):
        raise ValueError("benchmark metric requires schema_version=1 and unit=seconds")
    if any(
        not isinstance(data[key], str) or not data[key].strip() for key in ("metric", "workload_id")
    ):
        raise ValueError("benchmark metric and workload_id must be nonempty strings")
    value = data["value"]
    if type(value) not in (int, float) or not 0 < value < math.inf:
        raise ValueError("benchmark value must be finite positive seconds")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError("benchmark value exceeds finite seconds") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("benchmark value must be finite positive seconds")
    return BenchmarkResult(
        (data["metric"], data["unit"], data["workload_id"]), seconds, outer_seconds
    )


def normalize_benchmark_protected_paths(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(path, str)
        or not path.strip()
        or path != path.strip()
        or path.startswith(("/", "-"))
        or any(part in ("", ".", "..", ".git") for part in path.lower().split("/"))
        or any(char in path for char in "\\:*?[]")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        for path in value
    ):
        raise ValueError(
            "benchmarkProtectedPaths must be a list of explicit repository-relative files or roots; "
            "empty paths, traversal, .git, options and globs are not allowed"
        )
    return list(dict.fromkeys(value))


def benchmark_harness_globs(
    root: Path, commands: list[str], protected_paths: list[str] | None = None
) -> list[str]:
    protected = []
    root = root.resolve()
    for protected_path in normalize_benchmark_protected_paths(
        [] if protected_paths is None else protected_paths
    ):
        resolved = (root / protected_path).resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise ValueError("benchmarkProtectedPaths must stay inside the repository")
        relative = resolved.relative_to(root).as_posix()
        normalize_benchmark_protected_paths([relative])
        for spelling in dict.fromkeys((protected_path, relative)):
            protected.extend([spelling, spelling + "/**"])
    for command in commands:
        argv = shlex.split(command)
        if "-m" in argv:
            index = argv.index("-m") + 1
            if index < len(argv) and argv[index] != "pytest":
                module = argv[index].replace(".", "/")
                protected.extend(
                    [
                        module + ".py",
                        module + "/**",
                        "src/" + module + ".py",
                        "src/" + module + "/**",
                    ]
                )
        script = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else None
        for arg in argv[1:]:
            path = Path(arg.split("::", 1)[0])
            if path.suffix != ".py" and arg != script:
                continue
            resolved = (root / path).resolve()
            if resolved.is_relative_to(root.resolve()):
                protected.append(resolved.relative_to(root.resolve()).as_posix())
                if not path.is_absolute() and ".." not in path.parts:
                    protected.append(path.as_posix())
    return protected
