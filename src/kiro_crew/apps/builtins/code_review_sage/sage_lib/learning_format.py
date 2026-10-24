"""Deterministic learning-record validation, selection and markdown formatting."""

from __future__ import annotations

import collections
import hashlib
import json
import re
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from sage_lib import store  # noqa: E402

DEFAULT_NAMESPACE = "default"

LEARNING_RECORDS_SCHEMA = "code-review-sage-learning-records"

LEARNING_RECORDS_VERSION = 1

_CONSOLIDATION_ACTIONS = frozenset({"promote", "merge", "archive", "retain"})

_CONSOLIDATION_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")

_LEARNING_LIFECYCLES = frozenset({"candidate", "active", "archived", "pinned"})

_UNKNOWN_PROVENANCE = "unknown"

_PROMOTION_RECURRENCE_COUNT = 2

_ACTIVE_CONTEXT_BUDGETS = {
    "global": {"max_rules": 20, "max_tokens": 4000},
    "repository": {"max_rules": 12, "max_tokens": 2400},
}

# A valid user namespace token: lowercase alphanumeric start/end, with hyphens,
# dots and underscores in between, 2-64 chars. Deliberately excludes path
# separators and ".." so a namespace name can NEVER escape the namespaces/ dir.
# ASCII on purpose: the name IS the directory, so it must mean one thing on every
# platform and filesystem. A refusal names the offending character
# (`create_namespace`'s `char`) so the dashboard can explain the rule in the
# user's own language. The class is `kiro_crew.slugs.SLUG_CHARS`, which tests
# pin it to.
_NS_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")

_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")

_REPOSITORY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_BINDING_SCOPES = frozenset({"global", "repository"})


def _is_valid_ns_name(name: str) -> bool:
    """True if ``name`` is a safe user-namespace token (no path traversal)."""
    return bool(name) and ".." not in name and "/" not in name and bool(_NS_NAME_RE.match(name))


def _namespace_dir(namespace: str | None = None, root: Path | None = None) -> Path:
    """Resolve the directory for a namespace. 'default' (or None) -> common/.

    Any non-default name MUST be a valid namespace token; this is the single
    chokepoint that prevents a crafted name (e.g. '../common', absolute paths)
    from escaping the namespaces/ directory in any code path that targets a
    namespace (stage, consolidate, list, delete)."""
    ns = namespace or DEFAULT_NAMESPACE
    base = store.data_dir(root) / "learnings"
    if ns == DEFAULT_NAMESPACE:
        return base / "common"
    if not _is_valid_ns_name(ns):
        raise ValueError(f"invalid namespace name: {ns!r}")
    return base / "namespaces" / ns


class LearningRecordError(ValueError):
    """Raised when the durable learning-record sidecar is not trustworthy."""


LearningRecordError.__module__ = __name__.removesuffix("_format")


def _empty_learning_records_document() -> dict:
    return {
        "schema": LEARNING_RECORDS_SCHEMA,
        "version": LEARNING_RECORDS_VERSION,
        "records": [],
    }


def _record_error(message: str) -> LearningRecordError:
    return LearningRecordError(f"invalid learning-record sidecar: {message}")


def _validate_recurrence_evidence(evidence: object, count: object) -> None:
    if not isinstance(evidence, list):
        raise _record_error("record recurrence.evidence must be a list")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise _record_error("record recurrence.count must be a positive integer")
    reviews: set[str] = set()
    changes: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"review", "change", "observed_at"}:
            raise _record_error("recurrence evidence must contain review, change, and observed_at")
        for key in ("review", "change", "observed_at"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise _record_error(f"recurrence evidence {key!r} must be a non-empty string")
        if item["review"] in reviews or item["change"] in changes:
            raise _record_error("recurrence evidence must name independent reviews and changes")
        reviews.add(item["review"])
        changes.add(item["change"])
    if count != max(1, len(evidence)):
        raise _record_error("record recurrence.count must match independent evidence")


def _validate_learning_record(record: object) -> None:
    if not isinstance(record, dict):
        raise _record_error("record must be an object")
    for key in ("id", "text", "rule", "namespace", "scope", "lifecycle"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise _record_error(f"record {key!r} must be a non-empty string")
    if record["lifecycle"] not in _LEARNING_LIFECYCLES:
        raise _record_error(f"unknown lifecycle {record['lifecycle']!r}")
    origin = record.get("origin")
    if not isinstance(origin, dict) or not isinstance(origin.get("source"), str):
        raise _record_error("record origin.source must be a string")
    if origin.get("reference") is not None and not isinstance(origin.get("reference"), str):
        raise _record_error("record origin.reference must be a string or null")
    if record.get("repository_identity") is not None and not isinstance(
        record.get("repository_identity"), str
    ):
        raise _record_error("record repository_identity must be a string or null")
    timestamps = record.get("timestamps")
    if not isinstance(timestamps, dict):
        raise _record_error("record timestamps must be an object")
    for key in ("created_at", "updated_at", "archived_at"):
        if timestamps.get(key) is not None and not isinstance(timestamps.get(key), str):
            raise _record_error(f"record timestamps.{key} must be a string or null")
    recurrence = record.get("recurrence")
    if not isinstance(recurrence, dict):
        raise _record_error("record recurrence must be an object")
    if set(recurrence) != {"count", "evidence"}:
        raise _record_error("record recurrence must contain count and evidence")
    _validate_recurrence_evidence(recurrence.get("evidence"), recurrence.get("count"))
    if not isinstance(record.get("legacy"), bool):
        raise _record_error("record legacy must be a boolean")
    archived_from = record.get("archived_from")
    if archived_from is not None and archived_from not in {"active", "pinned"}:
        raise _record_error("record archived_from must be active, pinned, or null")


def validate_learning_records_document(document: object) -> None:
    """Validate the complete durable sidecar before it can replace any copy."""
    if not isinstance(document, dict):
        raise _record_error("document must be an object")
    if document.get("schema") != LEARNING_RECORDS_SCHEMA:
        raise _record_error("unexpected schema")
    if document.get("version") != LEARNING_RECORDS_VERSION:
        raise _record_error("unsupported version")
    records = document.get("records")
    if not isinstance(records, list):
        raise _record_error("records must be a list")
    ids: set[str] = set()
    for record in records:
        _validate_learning_record(record)
        record_id = record["id"]
        if record_id in ids:
            raise _record_error(f"duplicate record id {record_id!r}")
        ids.add(record_id)


def _read_learning_records_document(path: Path, namespace_dir: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return _empty_learning_records_document(), None
    try:
        raw = store.read_text_nolink(path, namespace_dir)
        document = json.loads(raw or "")
    except (OSError, ValueError) as exc:
        raise _record_error(str(exc)) from exc
    validate_learning_records_document(document)
    return document, raw


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_active_context_budgets(review: object) -> dict[str, dict[str, int]]:
    """Validate the bounded sidecar context configuration without repairing it."""
    if not isinstance(review, dict):
        raise ValueError("review configuration must be an object")
    raw = review.get("active_context", _ACTIVE_CONTEXT_BUDGETS)
    if not isinstance(raw, dict) or set(raw) != set(_ACTIVE_CONTEXT_BUDGETS):
        raise ValueError("review.active_context must contain global and repository budgets")
    normalized: dict[str, dict[str, int]] = {}
    for scope in sorted(_ACTIVE_CONTEXT_BUDGETS):
        budget = raw.get(scope)
        if not isinstance(budget, dict) or set(budget) != {"max_rules", "max_tokens"}:
            raise ValueError(f"review.active_context.{scope} must contain max_rules and max_tokens")
        max_rules = budget.get("max_rules")
        max_tokens = budget.get("max_tokens")
        if (
            isinstance(max_rules, bool)
            or not isinstance(max_rules, int)
            or max_rules < 1
            or isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
        ):
            raise ValueError(f"review.active_context.{scope} budgets must be positive integers")
        normalized[scope] = {"max_rules": max_rules, "max_tokens": max_tokens}
    return normalized


def _record_token_cost(record: dict) -> int:
    return max(1, (len(str(record["rule"]).encode("utf-8")) + 3) // 4)


def _record_recency(record: dict) -> str:
    timestamps = record.get("timestamps") or {}
    return str(timestamps.get("updated_at") or timestamps.get("created_at") or "")


def _record_context_scope(record: dict, default_scope: str = "global") -> str:
    if record.get("repository_identity") is not None:
        return "repository"
    return default_scope


def _rank_context_records(candidates: list[dict]) -> list[dict]:
    ranked = list(candidates)
    ranked.sort(key=lambda item: str(item["record"]["id"]))
    ranked.sort(key=lambda item: item["scope"] == "repository", reverse=True)
    ranked.sort(key=lambda item: _record_recency(item["record"]), reverse=True)
    ranked.sort(key=lambda item: item["record"]["recurrence"]["count"], reverse=True)
    ranked.sort(key=lambda item: item["record"]["lifecycle"] == "pinned", reverse=True)
    return ranked


def select_active_context(candidates: list[dict], budgets: dict[str, dict[str, int]]) -> dict:
    """Select a deterministic bounded context and retain every inclusion decision."""
    selected: list[dict] = []
    decisions: list[dict] = []
    usage = {scope: {"rules": 0, "tokens": 0} for scope in budgets}
    for candidate in _rank_context_records(candidates):
        record = candidate["record"]
        scope = candidate["scope"]
        tokens = _record_token_cost(record)
        reason = "selected"
        if usage["global"]["rules"] >= budgets["global"]["max_rules"]:
            reason = "excluded_global_rule_budget"
        elif usage["global"]["tokens"] + tokens > budgets["global"]["max_tokens"]:
            reason = "excluded_global_token_budget"
        elif scope == "repository" and usage[scope]["rules"] >= budgets[scope]["max_rules"]:
            reason = "excluded_repository_rule_budget"
        elif (
            scope == "repository" and usage[scope]["tokens"] + tokens > budgets[scope]["max_tokens"]
        ):
            reason = "excluded_repository_token_budget"
        decision = {
            "record_id": record["id"],
            "namespace": candidate["namespace"],
            "lifecycle": record["lifecycle"],
            "scope": scope,
            "token_cost": tokens,
            "reason": reason,
        }
        decisions.append(decision)
        if reason != "selected":
            continue
        selected.append(candidate)
        usage["global"]["rules"] += 1
        usage["global"]["tokens"] += tokens
        if scope == "repository":
            usage[scope]["rules"] += 1
            usage[scope]["tokens"] += tokens
    return {"selected": selected, "decisions": decisions, "usage": usage, "budgets": budgets}


def _legacy_record_id(namespace: str, lifecycle: str, occurrence: int, pattern: dict) -> str:
    payload = {
        "namespace": namespace,
        "lifecycle": lifecycle,
        "occurrence": occurrence,
        "title": pattern.get("title", ""),
        "scope": pattern.get("scope", ""),
        "guidance": pattern.get("guidance", ""),
        "added_at": pattern.get("added_at", ""),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "legacy-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _legacy_record(pattern: dict, namespace: str, lifecycle: str, occurrence: int) -> dict:
    rule = " ".join(str(pattern.get("guidance") or "").split())
    title = " ".join(str(pattern.get("title") or "").split())
    created_at = str(pattern.get("added_at") or "").strip() or None
    return {
        "id": _legacy_record_id(namespace, lifecycle, occurrence, pattern),
        "text": "\n".join(part for part in (title, rule) if part),
        "rule": rule or title,
        "namespace": namespace,
        "scope": str(pattern.get("scope") or "common"),
        "lifecycle": lifecycle,
        "origin": {"source": _UNKNOWN_PROVENANCE, "reference": None},
        "repository_identity": None,
        "timestamps": {"created_at": created_at, "updated_at": None, "archived_at": None},
        "recurrence": {"count": 1, "evidence": []},
        "legacy": True,
    }


def _read_markdown_patterns(path: Path, namespace_dir: Path) -> list[dict]:
    if not path.exists():
        return []
    return parse_patterns(store.read_text_nolink(path, namespace_dir) or "")


def canonical_repository_source(source: object) -> dict[str, str]:
    """Validate and canonicalize the repository identity used by a binding."""
    if not isinstance(source, dict):
        raise ValueError("repository source must be an object")
    expected = {"provider", "host", "owner", "repository"}
    if set(source) != expected:
        raise ValueError("repository source must contain provider, host, owner, and repository")
    provider = str(source["provider"] or "").strip().lower()
    host = str(source["host"] or "").strip().lower()
    owner = str(source["owner"] or "").strip().lower()
    repository = str(source["repository"] or "").strip().removesuffix(".git").lower()
    if provider != "github":
        raise ValueError(f"unsupported repository provider: {provider!r}")
    if host == "www.github.com":
        host = "github.com"
    if not _HOST_RE.fullmatch(host) or not (
        _REPOSITORY_SEGMENT_RE.fullmatch(owner) and _REPOSITORY_SEGMENT_RE.fullmatch(repository)
    ):
        raise ValueError("repository source has an invalid host, owner, or repository")
    return {"provider": provider, "host": host, "owner": owner, "repository": repository}


def validate_namespace_bindings(bindings: object) -> dict[str, dict]:
    """Validate the persisted namespace-binding configuration without changing it."""
    if bindings is None:
        return {}
    if not isinstance(bindings, dict):
        raise ValueError("namespace_bindings must be an object")
    normalized: dict[str, dict] = {}
    for namespace, binding in bindings.items():
        if not isinstance(namespace, str) or (
            namespace != DEFAULT_NAMESPACE and not _is_valid_ns_name(namespace)
        ):
            raise ValueError(f"invalid namespace binding name: {namespace!r}")
        if not isinstance(binding, dict) or not isinstance(binding.get("scope"), str):
            raise ValueError(f"binding for {namespace!r} must name a scope")
        scope = binding["scope"]
        if scope not in _BINDING_SCOPES:
            raise ValueError(f"binding for {namespace!r} has unsupported scope {scope!r}")
        if scope == "global":
            if set(binding) != {"scope"}:
                raise ValueError(f"global binding for {namespace!r} cannot name a repository")
            normalized[namespace] = {"scope": scope}
            continue
        if set(binding) != {"scope", "repository"}:
            raise ValueError(f"repository binding for {namespace!r} must name one repository")
        normalized[namespace] = {
            "scope": scope,
            "repository": canonical_repository_source(binding["repository"]),
        }
    return normalized


def pattern_id(title: str, scope: str) -> str:
    # A content-derived dedup key over (title, scope), recomputed on every load
    # rather than persisted, so the digest algorithm can change freely. SHA-256
    # keeps this off the "broken hash" security lint even though the value is
    # never a signature or credential.
    return hashlib.sha256(f"{title.strip().lower()}|{scope}".encode()).hexdigest()[:16]


def equivalence_key(title: str, scope: str, impact: str, guidance: str) -> str:
    """Exact content equivalence, stricter than ``pattern_id``.

    Two entries coalesce on staging only when title, scope, impact AND guidance
    all match after whitespace normalisation. The same identity with a changed
    guidance or impact is a different key, so both versions reach consolidation
    and nothing silently picks a winner. Like ``pattern_id`` it is recomputed on
    every load and never read from the file, so a hand edit changes it."""
    parts = [" ".join(str(v or "").split()) for v in (title, scope, impact, guidance)]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def render_pattern(p: dict) -> str:
    """Render a pattern as guidance-only markdown. A learned pattern is a single
    high-level, code-agnostic review heuristic: the title + guidance are the
    whole rule. If a rule needs a symptom anecdote or a concrete example to be
    understood, the guidance is underspecified — sharpen it instead."""
    occurrence = str(p.get("occurrence_id") or "").strip()
    occurrence_comment = f" <!-- sage-id:{occurrence} -->" if occurrence else ""
    seen = max(1, int(p.get("seen") or 1))
    last = str(p.get("last_seen_at") or "").strip()
    return (
        f"### {p['title']} <!-- scope:{p.get('scope', 'common')} -->"
        f" <!-- impact:{p.get('impact', 'medium')} -->"
        f" <!-- added:{p.get('added_at', '')} -->{occurrence_comment}"
        + (f" <!-- seen:{seen} -->" if seen > 1 else "")
        + (f" <!-- last:{last} -->" if last else "")
        + f"\n{' '.join(p.get('guidance', '').split())}\n"
    )


# The two metadata labels of the retired pattern format, in their label form
# (`**Symptom ...:**`, `**Example:**`), nothing broader: guidance that merely
# opens with the bold word "Example" is guidance.
_LEGACY_META = re.compile(r"\*\*(?:Symptom\b[^*\n]*|Example):\*\*")

_HDR = re.compile(
    r"###\s+(?P<title>.*?)\s*(?:<!--\s*scope:(?P<scope>\w+)\s*-->)?"
    r"\s*(?:<!--\s*impact:(?P<impact>\w+)\s*-->)?"
    r"\s*(?:<!--\s*added:(?P<added>[^>]*?)\s*-->)?"
    r"\s*(?:<!--\s*sage-id:(?P<occurrence>(?:[0-9a-f-]{36}|legacy-[0-9a-f]{24}))\s*-->)?"
    r"\s*(?:<!--\s*seen:(?P<seen>\d{1,9})\s*-->)?"
    r"\s*(?:<!--\s*last:(?P<last>[^>]*?)\s*-->)?\s*$"
)


def parse_patterns(md: str) -> list[dict]:
    """Parse a learned-patterns(.candidate).md file into pattern dicts (tolerant).

    Patterns are guidance-only. Legacy ``**Symptom ...:**`` / ``**Example:**``
    lines from the old format are ignored, so existing files keep parsing
    cleanly until the next consolidation rewrites them lean. Only those two
    labels are skipped: guidance that merely opens in bold is guidance, and
    dropping it would also make two different rules look equivalent."""
    out: list[dict] = []
    for block in re.split(r"^(?=### )", md or "", flags=re.M):
        if not block.startswith("### "):
            continue
        lines = block.splitlines()
        m = _HDR.match(lines[0])
        if not m:
            continue
        title = m.group("title").strip()
        scope = m.group("scope") or "common"
        guidance_lines: list[str] = []
        for ln in lines[1:]:
            s = ln.strip()
            # Accumulate every non-empty, non-metadata line as guidance; legacy
            # Symptom/Example lines are skipped, so the round-trip stays lossless
            # for multi-line guidance.
            if s and not _LEGACY_META.match(s):
                guidance_lines.append(s)
        pattern = {
            "id": pattern_id(title, scope),
            "title": title,
            "scope": scope,
            "impact": m.group("impact") or "medium",
            "added_at": (m.group("added") or "").strip(),
            "guidance": " ".join(guidance_lines),
        }
        occurrence = m.group("occurrence")
        pattern.update(
            key=equivalence_key(title, scope, pattern["impact"], pattern["guidance"]),
            seen=max(1, int(m.group("seen") or 1)),
            last_seen_at=(m.group("last") or "").strip(),
        )
        if occurrence:
            pattern["occurrence_id"] = occurrence
        out.append(pattern)
    return out


def _legacy_candidate_occurrence_id(namespace: str, occurrence: int, pattern: dict) -> str:
    """Derive a stable selector for duplicated pre-governance markdown entries."""
    return _legacy_record_id(namespace, "candidate", occurrence, pattern)


def _candidate_patterns_with_occurrence_ids(patterns: Sequence[dict], namespace: str) -> list[dict]:
    """Expose one selector per candidate without rewriting legacy markdown.

    Legacy candidate files identify rules by title and scope, so identical entries
    share their historical id. Only collisions receive a deterministic ordinal
    selector; unique legacy entries retain their public id while governed entries
    carry the durable UUID written at staging time.
    """
    counts = collections.Counter(str(item.get("id") or "") for item in patterns)
    occurrences: collections.Counter[str] = collections.Counter()
    resolved: list[dict] = []
    for pattern in patterns:
        item = dict(pattern)
        logical_id = str(item.get("id") or "")
        occurrences[logical_id] += 1
        if item.get("occurrence_id"):
            item["id"] = str(item["occurrence_id"])
        elif counts[logical_id] > 1 and occurrences[logical_id] > 1:
            occurrence_id = _legacy_candidate_occurrence_id(
                namespace, occurrences[logical_id], item
            )
            item["id"] = occurrence_id
            item["occurrence_id"] = occurrence_id
        resolved.append(item)
    return resolved


def _atomic_write(path: Path, text: str) -> None:
    store.mkdir_refusing_links(path.parent)
    store.atomic_write_text(path, text)


def _normalize_pattern(pattern: dict) -> dict:
    p = dict(pattern)
    p.setdefault("scope", "common")
    p.setdefault("impact", "medium")
    p.setdefault("added_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    p.setdefault("seed", False)
    p["id"] = str(p.get("id") or pattern_id(p.get("title", ""), p["scope"]))
    return p


_CANDIDATE_HEADER = (
    "# Pending learnings (candidate) — awaiting consolidation\n\n"
    '<!-- New learnings are appended here during reviews. Trigger "Consolidate" '
    "to AI-merge them into learned-patterns.md, after which this file is cleared. "
    "You may edit/curate this file directly before consolidating. -->\n\n"
)


def _sha256_json(value: object) -> str:
    """Return a stable digest for durable snapshots and generations."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _content_generation(raw: str | None) -> str:
    """Represent an absent file distinctly from an empty durable document."""
    return _sha256_json({"raw": raw, "present": raw is not None})


def consolidation_preview_dir(root: Path | None = None, namespace: str | None = None) -> Path:
    """Directory containing immutable, namespace-local consolidation proposals."""
    return _namespace_dir(namespace, root) / "consolidation-previews"


def _preview_path(preview_id: str, root: Path | None, namespace: str | None) -> Path:
    try:
        parsed = uuid.UUID(preview_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid consolidation preview id") from exc
    return consolidation_preview_dir(root, namespace) / (str(parsed) + ".v1.json")


def _read_current_text(path: Path, namespace_dir: Path) -> str | None:
    if not path.exists():
        return None
    return store.read_text_nolink(path, namespace_dir)


def _candidate_snapshot_entry(pattern: dict) -> dict:
    normalized = _normalize_pattern(pattern)
    return {
        # ``list_candidate`` may replace a legacy duplicate's historical
        # content id with an ordinal selector. Keep that selector in snapshots;
        # otherwise one preview decision would target every identical entry.
        "id": str(pattern.get("id") or normalized["id"]),
        "digest": _sha256_json(normalized),
        "pattern": normalized,
    }


def _candidate_snapshot_matches(snapshot: Sequence[dict], current: Sequence[dict]) -> bool:
    """Require every snapshotted record while allowing later append-only staging."""
    expected = collections.Counter(
        (str(item.get("id") or ""), str(item.get("digest") or "")) for item in snapshot
    )
    actual = collections.Counter(
        (entry["id"], entry["digest"])
        for entry in (_candidate_snapshot_entry(item) for item in current)
    )
    return all(actual[key] >= count for key, count in expected.items())


def _candidate_body_after_consuming(current: Sequence[dict], consumed: Sequence[dict]) -> str:
    """Keep unpreviewed and concurrently appended entries byte-independent of a preview."""
    budget = collections.Counter((item["id"], item["digest"]) for item in consumed)
    kept: list[dict] = []
    for pattern in current:
        entry = _candidate_snapshot_entry(pattern)
        key = (entry["id"], entry["digest"])
        if budget.get(key, 0):
            budget[key] -= 1
            continue
        kept.append(entry["pattern"])
    return _CANDIDATE_HEADER + "\n".join(render_pattern(item) for item in kept) + "\n"


def _matching_candidate_record_ids(records: Sequence[dict], pattern: dict) -> list[str]:
    """Find governed candidate records without making legacy markdown governing."""
    rule = " ".join(str(pattern.get("guidance") or "").split())
    return [
        str(record["id"])
        for record in records
        if record.get("lifecycle") == "candidate"
        and (
            record.get("id") == pattern.get("id")
            or " ".join(str(record.get("rule") or "").split()) == rule
        )
    ]


def _below_promotion_floor(record: dict) -> bool:
    """Whether promoting this record is what the lifecycle preview refuses.

    A rule the reviewer has met only once would let a single incident govern every
    later review, so the floor keeps the ruleset to heuristics that recurred. The
    preview and the merge prompt both read it here, so the worker is never handed a
    promotion the apply will reject.
    """
    return (
        record.get("lifecycle") == "candidate"
        and int((record.get("recurrence") or {}).get("count") or 0) < _PROMOTION_RECURRENCE_COUNT
    )


def _validate_consolidation_proposal(
    proposal: object, selected_ids: set[str]
) -> tuple[str, list[dict]]:
    """Accept only a complete, machine-readable worker proposal."""
    if not isinstance(proposal, dict) or set(proposal) != {"ruleset_markdown", "decisions"}:
        raise ValueError("malformed_worker_output")
    ruleset = proposal.get("ruleset_markdown")
    decisions = proposal.get("decisions")
    if (
        not isinstance(ruleset, str)
        or not parse_patterns(ruleset)
        or not isinstance(decisions, list)
    ):
        raise ValueError("malformed_worker_output")
    normalized: list[dict] = []
    ids: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict) or set(item) != {"candidate_id", "action", "reason_code"}:
            raise ValueError("malformed_worker_output")
        candidate_id = item.get("candidate_id")
        action = item.get("action")
        reason_code = item.get("reason_code")
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in selected_ids
            or candidate_id in ids
            or action not in _CONSOLIDATION_ACTIONS
            or not isinstance(reason_code, str)
            or not _CONSOLIDATION_REASON_CODE_RE.fullmatch(reason_code)
        ):
            raise ValueError("malformed_worker_output")
        ids.add(candidate_id)
        normalized.append(
            {"candidate_id": candidate_id, "action": action, "reason_code": reason_code}
        )
    if ids != selected_ids:
        raise ValueError("malformed_worker_output")
    return ruleset if ruleset.endswith("\n") else ruleset + "\n", normalized


__all__ = (
    "DEFAULT_NAMESPACE",
    "LEARNING_RECORDS_SCHEMA",
    "LEARNING_RECORDS_VERSION",
    "LearningRecordError",
    "_ACTIVE_CONTEXT_BUDGETS",
    "_BINDING_SCOPES",
    "_CANDIDATE_HEADER",
    "_CONSOLIDATION_ACTIONS",
    "_CONSOLIDATION_REASON_CODE_RE",
    "_HDR",
    "_HOST_RE",
    "_LEARNING_LIFECYCLES",
    "_LEGACY_META",
    "_NS_NAME_RE",
    "_PROMOTION_RECURRENCE_COUNT",
    "_REPOSITORY_SEGMENT_RE",
    "_UNKNOWN_PROVENANCE",
    "_atomic_write",
    "_below_promotion_floor",
    "_candidate_body_after_consuming",
    "_candidate_patterns_with_occurrence_ids",
    "_candidate_snapshot_entry",
    "_candidate_snapshot_matches",
    "_content_generation",
    "_empty_learning_records_document",
    "_is_valid_ns_name",
    "_legacy_candidate_occurrence_id",
    "_legacy_record",
    "_legacy_record_id",
    "_matching_candidate_record_ids",
    "_namespace_dir",
    "_normalize_pattern",
    "_preview_path",
    "_rank_context_records",
    "_read_current_text",
    "_read_learning_records_document",
    "_read_markdown_patterns",
    "_record_context_scope",
    "_record_error",
    "_record_recency",
    "_record_token_cost",
    "_sha256_json",
    "_utc_now",
    "_validate_consolidation_proposal",
    "_validate_learning_record",
    "_validate_recurrence_evidence",
    "canonical_repository_source",
    "consolidation_preview_dir",
    "equivalence_key",
    "parse_patterns",
    "pattern_id",
    "render_pattern",
    "select_active_context",
    "validate_active_context_budgets",
    "validate_learning_records_document",
    "validate_namespace_bindings",
)
