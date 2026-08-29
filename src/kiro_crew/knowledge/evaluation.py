"""Private golden-set evaluation for the Knowledge Library.

The public benchmark package measures cross-session memory against published
corpora. This module measures one operator's indexed documents, so its fixtures
and reports belong under the data home rather than the repository.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .retrieval import HybridRetriever
from .store import KnowledgeStore

GOLDEN_VERSION = 1
DEFAULT_LIMIT = 5
MIN_SCORE = 0.012


class GoldenSetError(RuntimeError):
    """A private evaluation fixture cannot yield an attributable measurement."""


@dataclass(frozen=True)
class ExpectedLocator:
    """A stable document locator for one expected result.

    Database item ids are deliberately absent: re-chunking changes them. A source
    URI plus a document, section, or excerpt anchor stays meaningful while still
    making an over-broad expectation impossible to hide.
    """

    source_uri: str
    file_path: str | None = None
    section_title: str | None = None
    content_contains: str | None = None

    def matches(self, result: dict[str, Any]) -> bool:
        source_uris = result.get("_evaluation_source_uris")
        if not isinstance(source_uris, set):
            source_uris = {result.get("source_uri")}
        if self.source_uri not in source_uris:
            return False
        if self.file_path is not None and result.get("file_path") != self.file_path:
            return False
        if self.section_title is not None and result.get("section_title") != self.section_title:
            return False
        if self.content_contains is not None:
            return self.content_contains in str(result.get("content") or "")
        return True


@dataclass(frozen=True)
class GoldenCase:
    """One query and its independently verified expected evidence."""

    case_id: str
    query: str
    category: str
    expected: tuple[ExpectedLocator, ...]
    scope_source_uri: str | None = None


@dataclass(frozen=True)
class GoldenSet:
    """A versioned, operator-private collection of retrieval assertions."""

    name: str
    cases: tuple[GoldenCase, ...]
    path: Path


def evaluation_dir(config_root: Path) -> Path:
    """Return the private, data-home-owned directory for fixtures and reports."""
    return config_root / "workspace" / "knowledge" / "evals"


def resolve_private_eval_path(config_root: Path, raw: str) -> Path:
    """Resolve a fixture/report path beneath the private evaluation directory."""
    base = evaluation_dir(config_root).resolve()
    candidate = (base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise GoldenSetError(
            "knowledge evaluation files must stay under the private eval directory"
        ) from exc
    return candidate


def _expect_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoldenSetError(f"golden-set field {field!r} must be a non-empty string")
    return value


def _parse_locator(raw: object, case_id: str, index: int) -> ExpectedLocator:
    if not isinstance(raw, dict):
        raise GoldenSetError(f"case {case_id!r} expected[{index}] must be an object")
    source_uri = _expect_string(raw.get("source_uri"), f"{case_id}.expected[{index}].source_uri")
    optional = {}
    for field in ("file_path", "section_title", "content_contains"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise GoldenSetError(f"case {case_id!r} {field!r} must be a string when present")
        optional[field] = value or None
    if not any(optional.values()):
        raise GoldenSetError(
            f"case {case_id!r} expected[{index}] needs a file, section, or excerpt anchor"
        )
    return ExpectedLocator(source_uri=source_uri, **optional)


def load_golden_set(path: Path) -> GoldenSet:
    """Load and validate a private golden-set JSON document before any search runs."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GoldenSetError("golden set was not found") from None
    except json.JSONDecodeError as exc:
        raise GoldenSetError(f"golden set is not valid JSON: {exc.msg}") from None
    if not isinstance(raw, dict):
        raise GoldenSetError("golden set root must be an object")
    if raw.get("version") != GOLDEN_VERSION:
        raise GoldenSetError(f"golden set must declare version {GOLDEN_VERSION}")
    name = _expect_string(raw.get("name"), "name")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise GoldenSetError("golden set needs at least one case")
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for index, case_raw in enumerate(cases_raw):
        if not isinstance(case_raw, dict):
            raise GoldenSetError(f"case {index} must be an object")
        case_id = _expect_string(case_raw.get("id"), f"cases[{index}].id")
        if case_id in seen:
            raise GoldenSetError(f"duplicate golden case id {case_id!r}")
        seen.add(case_id)
        expected_raw = case_raw.get("expected")
        if not isinstance(expected_raw, list):
            raise GoldenSetError(f"case {case_id!r} expected must be a list")
        expected = tuple(
            _parse_locator(value, case_id, pos) for pos, value in enumerate(expected_raw)
        )
        scope = case_raw.get("scope_source_uri")
        if scope is not None and not isinstance(scope, str):
            raise GoldenSetError(f"case {case_id!r} scope_source_uri must be a string")
        cases.append(
            GoldenCase(
                case_id=case_id,
                query=_expect_string(case_raw.get("query"), f"{case_id}.query"),
                category=_expect_string(case_raw.get("category"), f"{case_id}.category"),
                expected=expected,
                scope_source_uri=scope or None,
            )
        )
    return GoldenSet(name=name, cases=tuple(cases), path=path)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


def _ndcg(relevant: list[bool], expected_count: int, limit: int) -> float:
    if not expected_count:
        return 0.0
    dcg = sum(
        (1.0 if hit else 0.0) / math.log2(index + 2) for index, hit in enumerate(relevant[:limit])
    )
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(expected_count, limit)))
    return dcg / ideal if ideal else 0.0


def _source_ids(store: KnowledgeStore) -> dict[str, str]:
    rows = store.db.execute("SELECT id, uri FROM sources").fetchall()
    return {str(row["uri"]): str(row["id"]) for row in rows}


def _scope_item_ids(store: KnowledgeStore, source_id: str) -> set[str]:
    rows = store.db.execute(
        "SELECT i.id FROM items i WHERE i.status = 'active' AND "
        "(i.source_id = ? OR i.id IN "
        "(SELECT item_id FROM source_locations WHERE source_id = ?))",
        (source_id, source_id),
    ).fetchall()
    return {str(row["id"]) for row in rows}


def _attach_evaluation_source_uris(store: KnowledgeStore, results: list[dict[str, Any]]) -> None:
    """Attach every holding source URI so dedup does not invalidate a label.

    Retrieval displays the owning source's citation. A collapsed document can
    nevertheless be held by another source through ``source_locations``; a
    stable golden locator for that source must still match the returned chunk.
    This evaluator-only metadata is omitted from the private report.
    """
    item_ids = [str(row["id"]) for row in results if row.get("id")]
    if not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    by_item: dict[str, set[str]] = {item_id: set() for item_id in item_ids}
    owner_rows = store.db.execute(
        "SELECT i.id AS item_id, s.uri FROM items i "
        "LEFT JOIN sources s ON s.id = i.source_id "
        f"WHERE i.id IN ({placeholders})",  # noqa: S608
        item_ids,
    ).fetchall()
    location_rows = store.db.execute(
        "SELECT sl.item_id, s.uri FROM source_locations sl "
        "JOIN sources s ON s.id = sl.source_id "
        f"WHERE sl.item_id IN ({placeholders})",  # noqa: S608
        item_ids,
    ).fetchall()
    for row in [*owner_rows, *location_rows]:
        if row["uri"]:
            by_item[str(row["item_id"])].add(str(row["uri"]))
    for result in results:
        result["_evaluation_source_uris"] = by_item.get(str(result.get("id")), set())


def _result_record(result: dict[str, Any], matched: bool) -> dict[str, object]:
    return {
        "id": result.get("id"),
        "title": result.get("title"),
        "source_uri": result.get("source_uri"),
        "file_path": result.get("file_path"),
        "section_title": result.get("section_title"),
        "score": result.get("score"),
        "matched": matched,
    }


def evaluate_golden_set(
    store: KnowledgeStore,
    golden: GoldenSet,
    *,
    embedder: Callable[[str], list[float] | None] | None = None,
    embedding_signature: str | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
) -> dict[str, object]:
    """Run a golden set through production retrieval and return an attributable report."""
    if limit < 1:
        raise GoldenSetError("limit must be positive")
    source_ids = _source_ids(store)
    for case in golden.cases:
        for locator in case.expected:
            if locator.source_uri not in source_ids:
                raise GoldenSetError(
                    f"case {case.case_id!r} expected source is not registered; refresh its fixture"
                )
    retriever = HybridRetriever(store, embedder=embedder)
    scored: list[dict[str, object]] = []
    latencies: list[float] = []
    scope_cases = 0
    scope_leaks = 0
    for case in golden.cases:
        source_id = None
        scoped_ids: set[str] | None = None
        if case.scope_source_uri is not None:
            source_id = source_ids.get(case.scope_source_uri)
            if source_id is None:
                raise GoldenSetError(
                    f"case {case.case_id!r} scope source is not registered; refresh its fixture"
                )
            scoped_ids = _scope_item_ids(store, source_id)
            scope_cases += 1
        started = time.perf_counter()
        results = retriever.search(case.query, limit=limit, source_id=source_id)
        latency_ms = (time.perf_counter() - started) * 1000
        latencies.append(latency_ms)
        results = [row for row in results if float(row.get("score") or 0.0) >= min_score]
        _attach_evaluation_source_uris(store, results)
        matched_by_rank: list[set[int]] = []
        for result in results:
            matched_by_rank.append(
                {index for index, locator in enumerate(case.expected) if locator.matches(result)}
            )
        found = set().union(*matched_by_rank) if matched_by_rank else set()
        first_rank = next((index + 1 for index, hit in enumerate(matched_by_rank) if hit), None)
        leak = scoped_ids is not None and any(
            str(result.get("id")) not in scoped_ids for result in results
        )
        if leak:
            scope_leaks += 1
        scored.append(
            {
                "id": case.case_id,
                "category": case.category,
                "query": case.query,
                "scope_source_uri": case.scope_source_uri,
                "expected_count": len(case.expected),
                "matched_expected": len(found),
                "first_relevant_rank": first_rank,
                "latency_ms": round(latency_ms, 3),
                "scope_leak": leak,
                "results": [
                    _result_record(result, bool(matches))
                    for result, matches in zip(results, matched_by_rank)
                ],
            }
        )

    answerable = [case for case in scored if int(case["expected_count"]) > 0]
    abstention = [case for case in scored if int(case["expected_count"]) == 0]
    hit_rate = sum(1 for case in answerable if int(case["matched_expected"]) > 0)
    expected_total = sum(int(case["expected_count"]) for case in answerable)
    found_total = sum(int(case["matched_expected"]) for case in answerable)
    mrr = sum(
        1.0 / int(case["first_relevant_rank"])
        for case in answerable
        if case["first_relevant_rank"] is not None
    )
    ndcg = 0.0
    for case in answerable:
        relevant = [bool(row["matched"]) for row in case["results"]]  # type: ignore[index]
        ndcg += _ndcg(relevant, int(case["expected_count"]), limit)
    abstained = sum(1 for case in abstention if not case["results"])
    item_row = store.db.execute(
        "SELECT COUNT(*) AS count, MAX(updated_at) AS latest FROM items WHERE status = 'active'"
    ).fetchone()
    return {
        "schema_version": GOLDEN_VERSION,
        "golden_set": golden.name,
        "generated_at": datetime.now().isoformat(),
        "config": {
            "limit": limit,
            "min_score": min_score,
            "embedding_signature": embedding_signature,
        },
        "library": {"active_items": item_row["count"], "latest_item_update": item_row["latest"]},
        "metrics": {
            "answerable_cases": len(answerable),
            "abstention_cases": len(abstention),
            "hit_at_k": hit_rate / len(answerable) if answerable else 0.0,
            "recall_at_k": found_total / expected_total if expected_total else 0.0,
            "mrr": mrr / len(answerable) if answerable else 0.0,
            "ndcg_at_k": ndcg / len(answerable) if answerable else 0.0,
            "abstention_accuracy": abstained / len(abstention) if abstention else None,
            "scope_leak_rate": scope_leaks / scope_cases if scope_cases else None,
            "p50_latency_ms": round(_percentile(latencies, 0.5), 3),
            "p95_latency_ms": round(_percentile(latencies, 0.95), 3),
        },
        "cases": scored,
    }


def write_report(report: dict[str, object], directory: Path, stem: str | None = None) -> Path:
    """Write a private JSON report with an atomically replaced destination."""
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    filename = stem or f"report-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if not filename.endswith(".json"):
        filename += ".json"
    if Path(filename).name != filename or filename in {".", ".."}:
        raise GoldenSetError("report filename must not contain a path")
    destination = (directory / filename).resolve()
    try:
        destination.relative_to(directory)
    except ValueError as exc:
        raise GoldenSetError("report filename must stay under the private eval directory") from exc
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def report_summary(report: dict[str, object]) -> str:
    """Render the non-sensitive aggregate report for CLI output."""
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    return (
        f"Knowledge retrieval: hit@{report['config']['limit']}={metrics['hit_at_k']:.3f} "
        f"recall={metrics['recall_at_k']:.3f} MRR={metrics['mrr']:.3f} "
        f"nDCG={metrics['ndcg_at_k']:.3f} p95={metrics['p95_latency_ms']:.1f}ms"
    )
