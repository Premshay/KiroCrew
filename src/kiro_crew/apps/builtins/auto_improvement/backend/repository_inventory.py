"""Bounded static inventory of committed source and test evidence, never imports."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Callable

_SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb"}
_CONTAINERS = {"src", "apps", "packages", "services", "backend", "frontend"}
_EXCLUDED = {"test", "tests", "docs", "scripts", "benchmarks", "node_modules", "vendor"}
_MAX_AREAS = 200
_MAX_IMPORT_FILES = 256
_MAX_IMPORT_BYTES = 1024 * 1024


def _test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        any(part in {"test", "tests", "__tests__"} for part in parts[:-1])
        or parts[-1].startswith("test_")
        or parts[-1] == "tests.py"
        or ".test." in parts[-1]
        or ".spec." in parts[-1]
    )


def _root(path: str, app_roots: set[str]) -> str:
    parts = path.split("/")
    apps = [root for root in app_roots if path.startswith(root + "/")]
    if apps:
        return max(apps, key=len)
    index = 0
    while index < len(parts) - 2 and parts[index] in _CONTAINERS:
        index += 1
    return "/".join(parts[: index + 1])


def _compact_tests(root: str, focused: set[str]) -> list[str]:
    """Keep recognized test directories, retaining cross-area imported tests separately."""
    name = root.rsplit("/", 1)[-1]
    directories = set()
    for path in focused:
        parts = path.split("/")
        for index, part in enumerate(parts[:-1]):
            if part in {"test", "tests", "__tests__"} and path.startswith(root + "/"):
                directories.add("/".join(parts[: index + 1]))
                break
            if part == name and any(p in {"test", "tests", "__tests__"} for p in parts[:index]):
                directories.add("/".join(parts[: index + 1]))
                break
    paths = sorted(directories | focused)
    return [p for p in paths if not any(p.startswith(d + "/") for d in directories if d != p)]


def repository_inventory(entries: dict, read_blob: Callable[[str], str]) -> dict:
    """Expose unmapped areas and filename candidates without claiming coverage."""
    regular = sorted(p for p, entry in entries.items() if entry[0] in {"100644", "100755"})
    app_roots = {p.rsplit("/", 1)[0] for p in regular if p.endswith("/apps.py")}
    sources: dict[str, list[str]] = defaultdict(list)
    tests = []
    for path in regular:
        parts = path.split("/")
        if PurePosixPath(path).suffix not in _SOURCE_SUFFIXES:
            continue
        if _test(path):
            tests.append(path)
        elif not parts[0].startswith(".") and parts[0] not in _EXCLUDED:
            sources[_root(path, app_roots)].append(path)

    imports: dict[str, list[str]] = defaultdict(list)
    scanned = used_bytes = skipped = 0
    for path in tests:
        if not path.endswith(".py"):
            continue
        size = entries[path][2]
        if scanned >= _MAX_IMPORT_FILES or size > 65536 or used_bytes + size > _MAX_IMPORT_BYTES:
            skipped += 1
            continue
        scanned += 1
        used_bytes += size
        try:
            tree = ast.parse(read_blob(path), filename=path)
        except (SyntaxError, ValueError, RecursionError):
            skipped += 1
            continue
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                modules = [node.module] + [node.module + "." + alias.name for alias in node.names]
            for module in modules:
                imports[module].append(path)

    areas = []
    for root in sorted(sources)[:_MAX_AREAS]:
        name = root.rsplit("/", 1)[-1]
        focused = set()
        evidence = [f"Tracked source files in {root}; not a priority or coverage assessment"]
        for test in tests:
            if test.startswith(root + "/"):
                focused.add(test)
            elif any(part == name for part in test.split("/")[:-1]):
                focused.add(test)
        confidence = "directory" if focused else "unmapped"
        if focused:
            evidence.append("Test paths share the source directory or its name")
        source_modules = set()
        for source in sources[root]:
            if source.endswith(".py"):
                module = source[:-3].replace("/", ".").removesuffix(".__init__")
                source_modules.add(module)
                source_modules.add(module.removeprefix("src."))
        imported = {test for module in source_modules for test in imports.get(module, [])}
        if imported:
            focused.update(imported)
            confidence = "imports"
            evidence.append(
                "Static Python imports reference tracked source modules; coverage is unverified"
            )
        if not focused:
            evidence.append("No test mapping found; manual investigation is needed")
        if any(p.startswith(root + "/") and entry[0] == "120000" for p, entry in entries.items()):
            evidence.append("Area contains symlinks; automatic workload creation is disabled")
        mapped_paths = _compact_tests(root, focused)
        if len(mapped_paths) > 100:
            evidence.append("Mapping exceeds 100 paths; no automatic workload is offered")
        areas.append(
            {
                "id": root.replace("/", "-"),
                "path": root,
                "label": name,
                "kind": "django-app" if root in app_roots else "source-area",
                "sourceFiles": len(sources[root]),
                "testPaths": mapped_paths[:100],
                "totalTestPaths": len(mapped_paths),
                "totalTestFiles": len(focused),
                "testPathsTruncated": len(mapped_paths) > 100,
                "pythonSupported": any(p.endswith(".py") for p in sources[root]),
                "mappingConfidence": confidence,
                "evidence": evidence,
            }
        )
    performance = [
        p for p in regular if any(t in p.lower() for t in ("benchmark", "test_perf", "performance"))
    ]
    return {
        "areas": areas,
        "performanceCandidates": [
            {
                "path": p,
                "status": "unverified",
                "evidence": [
                    "Filename suggests performance relevance; no command or measurement verified"
                ],
            }
            for p in performance[:100]
        ],
        "inventoryLimits": {
            "maxAreas": _MAX_AREAS,
            "totalAreas": len(sources),
            "maxImportFiles": _MAX_IMPORT_FILES,
            "importFilesScanned": scanned,
            "importFilesSkipped": skipped,
            "maxImportBytes": _MAX_IMPORT_BYTES,
            "maxPerformanceCandidates": 100,
            "totalPerformanceCandidates": len(performance),
            "truncated": len(sources) > _MAX_AREAS
            or bool(skipped)
            or len(performance) > 100
            or any(area["testPathsTruncated"] for area in areas),
            "maxTestPathsPerArea": 100,
            "limitations": [
                "Static source inventory, not runtime profiling or test coverage",
                "Dynamic and relative imports are not resolved",
                "Django apps identified by tracked apps.py, not executed settings",
                "Source inventory excludes dot directories and top-level "
                + ", ".join(sorted(_EXCLUDED)),
                "Supported source extensions: " + ", ".join(sorted(_SOURCE_SUFFIXES)),
            ],
        },
    }
