#!/usr/bin/env python3
"""Learning system V2 — file-centric with namespace support.

Learning is **detection-gap (miss) analysis**: issues that shipped, traced back
to the introducing change, turned into a forward-looking pattern. The *judgment*
(why was it missed? which dimension was blind? how to merge it cleanly into the
ruleset?) is the LLM's job in the `learn-from-sage` skill. This module is the
deterministic backbone for the file-centric flow:

- pattern <-> markdown (the on-disk pattern format),
- ``stage_learning`` — cheap append of a new learning to the **candidate** file
  (``learned-patterns.candidate.md``); no model call, admissible-sources only,
- ``consolidate_apply`` — atomically replace ``learned-patterns.md`` with the
  AI-merged result and clear the candidate (the AI does the one-shot merge),
- ``learned-patterns.md`` is the ONLY file reviews load as heuristics; the
  candidate is pure staging until a human triggers consolidation.

Namespaces: learnings are grouped by namespace. The "default" namespace maps to
``data/learnings/common/`` (backward compatible). User-created namespaces live
under ``data/learnings/namespaces/<name>/``. Reviews load patterns from the
configured active namespace(s).
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/learning.py` (run as script)
    sys.path.insert(0, _APP_ROOT)

from sage_lib import store  # noqa: E402

from kiro_crew import platform_compat  # noqa: E402

# Learning is mined ONLY from human-validated, ground-truth signals.
ADMISSIBLE_SOURCES = {"fix_introduce", "human_comment", "design_outcome", "import"}

DEFAULT_NAMESPACE = "default"

LEARNING_RECORDS_SCHEMA = "code-review-sage-learning-records"
LEARNING_RECORDS_VERSION = 1
_LEARNING_LIFECYCLES = frozenset({"candidate", "active", "archived", "pinned"})
_UNKNOWN_PROVENANCE = "unknown"

# A valid user namespace token: lowercase alphanumeric start/end, with hyphens,
# dots and underscores in between, 2-64 chars. Deliberately excludes path
# separators and ".." so a namespace name can NEVER escape the namespaces/ dir.
_NS_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_REPOSITORY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_BINDING_SCOPES = frozenset({"global", "repository"})


def _is_valid_ns_name(name: str) -> bool:
    """True if ``name`` is a safe user-namespace token (no path traversal)."""
    return bool(name) and ".." not in name and "/" not in name and bool(_NS_NAME_RE.match(name))


# ---------------------------------------------------------------------------
# Paths — namespace-aware
# ---------------------------------------------------------------------------

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


def common_file(root: Path | None = None, namespace: str | None = None) -> Path:
    """The canonical, consolidated learnings file — the ONLY file reviews read."""
    return _namespace_dir(namespace, root) / "learned-patterns.md"


def candidate_file(root: Path | None = None, namespace: str | None = None) -> Path:
    """Append-only staging for new learnings, awaiting AI consolidation."""
    return _namespace_dir(namespace, root) / "learned-patterns.candidate.md"


def learning_records_file(root: Path | None = None, namespace: str | None = None) -> Path:
    """Versioned sidecar for governance metadata; markdown remains review input."""
    return _namespace_dir(namespace, root) / "learning-records.v1.json"


def learning_records_backup_file(root: Path | None = None, namespace: str | None = None) -> Path:
    """The last validated sidecar state before an explicit export changes it."""
    path = learning_records_file(root, namespace)
    return path.with_name(path.name + ".pre-export")


class LearningRecordError(ValueError):
    """Raised when the durable learning-record sidecar is not trustworthy."""


def _empty_learning_records_document() -> dict:
    return {
        "schema": LEARNING_RECORDS_SCHEMA,
        "version": LEARNING_RECORDS_VERSION,
        "records": [],
    }


def _record_error(message: str) -> LearningRecordError:
    return LearningRecordError(f"invalid learning-record sidecar: {message}")


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
    count = recurrence.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise _record_error("record recurrence.count must be a positive integer")
    if not isinstance(recurrence.get("evidence"), list):
        raise _record_error("record recurrence.evidence must be a list")
    if not isinstance(record.get("legacy"), bool):
        raise _record_error("record legacy must be a boolean")


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
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise _record_error(str(exc)) from exc
    validate_learning_records_document(document)
    return document, raw


def load_learning_records(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    """Read durable records without migrating or otherwise changing disk state."""
    document, _raw = _read_learning_records_document(
        learning_records_file(root, namespace), _namespace_dir(namespace, root)
    )
    return list(document["records"])


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


def export_learning_records(root: Path | None = None, namespace: str | None = None) -> dict:
    """Add legacy markdown entries to the versioned sidecar without touching markdown.

    This is an explicit migration/export operation. It never runs on import or a
    normal read, retains valid existing records, and snapshots the old sidecar
    before publishing a merged replacement.
    """
    ns = namespace or DEFAULT_NAMESPACE
    ns_dir = _namespace_dir(namespace, root)
    records_path = learning_records_file(root, namespace)
    document, raw = _read_learning_records_document(records_path, ns_dir)
    exported: list[dict] = []
    for lifecycle, path in (
        ("active", common_file(root, namespace)),
        ("candidate", candidate_file(root, namespace)),
    ):
        occurrences: collections.Counter[str] = collections.Counter()
        for pattern in _read_markdown_patterns(path, ns_dir):
            identity = _legacy_record_id(ns, lifecycle, 0, pattern)
            occurrences[identity] += 1
            exported.append(_legacy_record(pattern, ns, lifecycle, occurrences[identity]))
    existing_ids = {record["id"] for record in document["records"]}
    additions = [record for record in exported if record["id"] not in existing_ids]
    if not additions:
        return {
            "ok": True,
            "path": str(records_path),
            "added": 0,
            "records": len(document["records"]),
            "changed": False,
        }
    updated = dict(document)
    updated["records"] = [*document["records"], *additions]
    validate_learning_records_document(updated)
    ns_dir.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if raw is not None:
        backup = learning_records_backup_file(root, namespace)
        _atomic_write(backup, raw)
    _atomic_write(records_path, json.dumps(updated, indent=2, sort_keys=True) + "\n")
    result = {
        "ok": True,
        "path": str(records_path),
        "added": len(additions),
        "records": len(updated["records"]),
        "changed": True,
    }
    if backup is not None:
        result["backup"] = str(backup)
    return result


def migrate_legacy_learning_records(root: Path | None = None, namespace: str | None = None) -> dict:
    """Explicitly migrate compatible markdown entries into the record sidecar."""
    return export_learning_records(root, namespace)


def rollback_learning_records_export(
    root: Path | None = None, namespace: str | None = None
) -> dict:
    """Restore the validated pre-export sidecar without changing markdown files."""
    ns_dir = _namespace_dir(namespace, root)
    records_path = learning_records_file(root, namespace)
    backup_path = learning_records_backup_file(root, namespace)
    if not backup_path.exists():
        return {"ok": False, "error": "no pre-export sidecar snapshot exists"}
    _current, _current_raw = _read_learning_records_document(records_path, ns_dir)
    backup, backup_raw = _read_learning_records_document(backup_path, ns_dir)
    if backup_raw is None:
        raise _record_error("pre-export snapshot is empty")
    validate_learning_records_document(backup)
    _atomic_write(records_path, backup_raw)
    return {"ok": True, "path": str(records_path), "records": len(backup["records"])}


# One lock per (root, namespace) candidate file. `stage_learning` and the selective
# `clear_candidate` both read-modify-write it, and an atomic write does not make the
# whole sequence atomic: concurrent stagers would each read the same "before" text
# and the last write would drop the other's learning.
_CANDIDATE_LOCKS: dict[str, threading.Lock] = {}
_CANDIDATE_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _candidate_lock(root: Path | None, namespace: str | None):
    """Serialize a candidate read-modify-write against threads AND processes.

    A `threading.Lock` alone is not enough: reviews run as separate PROCESSES, and
    two of them staging a learning for the same namespace would each read the same
    "before" text and the last atomic write would drop the other's entry. The
    in-process lock still earns its place -- it is cheap and covers the gateway's own
    threads -- but the advisory file lock is what makes the sequence exclusive across
    the workers that actually produce learnings.
    """
    key = str(candidate_file(root, namespace))
    with _CANDIDATE_LOCKS_GUARD:
        lock = _CANDIDATE_LOCKS.get(key)
        if lock is None:
            lock = _CANDIDATE_LOCKS[key] = threading.Lock()
    ns_dir = _namespace_dir(namespace, root)
    ns_dir.mkdir(parents=True, exist_ok=True)
    # A dedicated lock file, never the catalog itself: locking the file being
    # atomically REPLACED would hold a lock on an inode the rename discards.
    lock_path = ns_dir / "candidate.md.lock"
    with lock:
        # `open(path, "w")` would be destructive here: it follows symlinks AND
        # truncates, so a worker that plants this name as a link to any file this
        # user can write erases that file just by us taking the lock. O_NOFOLLOW
        # refuses the link outright, no O_TRUNC because a lock's contents are
        # irrelevant, and the fstat rejects anything that is not a lone regular
        # file -- a hardlink to a sensitive inode passes O_NOFOLLOW but not
        # `st_nlink == 1`.
        #
        # Windows has no O_NOFOLLOW, and naming it unconditionally raised
        # AttributeError there -- taking the lock at all, so staging and
        # consolidation failed outright rather than degrading. Where the flag is
        # missing an lstat before the open carries the refusal: that is a weaker
        # TOCTOU story than the atomic flag, which is why the flag is still
        # preferred wherever the platform has it, and why the fstat below runs on
        # every platform as the check that cannot be raced.
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow and lock_path.is_symlink():
            raise OSError(f"refusing to lock {lock_path}: symlink")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise OSError(
                    f"refusing to lock {lock_path}: not a lone regular file")
            with platform_compat.file_lock(fd, exclusive=True):
                yield
        finally:
            os.close(fd)


def _consolidations_log(root: Path | None = None) -> Path:
    return store.data_dir(root) / "learnings" / "consolidations.jsonl"


# ---------------------------------------------------------------------------
# Namespace management
# ---------------------------------------------------------------------------

def list_namespaces(root: Path | None = None) -> list[str]:
    """Return all available namespace names. 'default' is always present."""
    namespaces = [DEFAULT_NAMESPACE]
    ns_dir = store.data_dir(root) / "learnings" / "namespaces"
    if ns_dir.is_dir():
        for d in sorted(ns_dir.iterdir()):
            if d.is_dir() and (d / "learned-patterns.md").exists():
                namespaces.append(d.name)
    return namespaces


def create_namespace(name: str, root: Path | None = None) -> dict:
    """Create a new namespace with an empty learnings file."""
    name = name.strip().lower().replace(" ", "-")
    if not _is_valid_ns_name(name):
        return {"ok": False, "error": f"invalid namespace name: {name!r} (use lowercase alphanumeric, hyphens, dots, 2-64 chars)"}
    if name == DEFAULT_NAMESPACE:
        return {"ok": False, "error": "'default' namespace already exists (it maps to common/)"}
    ns_path = _namespace_dir(name, root)
    if (ns_path / "learned-patterns.md").exists():
        return {"ok": False, "error": f"namespace {name!r} already exists"}
    ns_path.mkdir(parents=True, exist_ok=True)
    header = f"# Learned patterns — namespace: {name}\n\n"
    (ns_path / "learned-patterns.md").write_text(header, encoding="utf-8")
    return {"ok": True, "namespace": name, "path": str(ns_path)}


def delete_namespace(name: str, root: Path | None = None) -> dict:
    """Delete a user-created namespace. Cannot delete 'default'. Validates the
    name and confirms the resolved path is contained under namespaces/ before
    removing anything (defense-in-depth against path traversal)."""
    if name == DEFAULT_NAMESPACE:
        return {"ok": False, "error": "cannot delete the default namespace"}
    if not _is_valid_ns_name(name):
        return {"ok": False, "error": f"invalid namespace name: {name!r}"}
    ns_root = (store.data_dir(root) / "learnings" / "namespaces").resolve()
    ns_path = _namespace_dir(name, root).resolve()
    # Containment guard: the resolved target MUST live directly under namespaces/.
    if ns_path.parent != ns_root:
        return {"ok": False, "error": f"refusing to delete out-of-tree path for {name!r}"}
    if not ns_path.is_dir():
        return {"ok": False, "error": f"namespace {name!r} does not exist"}
    shutil.rmtree(ns_path)
    return {"ok": True, "deleted": name}


def get_active_namespaces(root: Path | None = None) -> list[str]:
    """Read the active namespaces from config. Defaults to ['default']."""
    cfg = store.load_config(root)
    return cfg.get("review", {}).get("active_namespaces", [DEFAULT_NAMESPACE])


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


def _sidecar_rule_ids(namespace: str, pattern: dict, root: Path | None) -> tuple[list[str], str | None]:
    """Return optional SAGE-1 record ids without making sidecar export implicit."""
    try:
        records = load_learning_records(root, namespace)
    except LearningRecordError as exc:
        return [], str(exc)
    rule = " ".join(str(pattern.get("guidance") or "").split())
    ids = [
        str(record["id"])
        for record in records
        if record.get("lifecycle") in {"active", "pinned"}
        and " ".join(str(record.get("rule") or "").split()) == rule
    ]
    return sorted(ids), None


def resolve_effective_rules(
    source_identity: dict[str, str] | None,
    *,
    config: dict | None = None,
    root: Path | None = None,
) -> dict:
    """Resolve eligible namespace rules and retain enough provenance for a later UI.

    Legacy active namespaces have no binding and remain globally applicable for
    compatibility. They are labelled separately from an explicit global binding
    so callers can present a migration action without changing review behaviour.
    """
    cfg = config if config is not None else store.load_config(root)
    review = cfg.get("review") if isinstance(cfg, dict) else {}
    review = review if isinstance(review, dict) else {}
    raw_active = review.get("active_namespaces", [DEFAULT_NAMESPACE])
    active = raw_active if isinstance(raw_active, list) else [DEFAULT_NAMESPACE]
    try:
        bindings = validate_namespace_bindings(review.get("namespace_bindings"))
        binding_error = None
    except ValueError as exc:
        bindings = {}
        binding_error = str(exc)
    try:
        canonical_source = canonical_repository_source(source_identity) if source_identity else None
    except ValueError:
        canonical_source = None

    available = set(list_namespaces(root))
    namespaces: list[dict] = []
    effective_namespaces: list[str] = []
    warnings: list[str] = [binding_error] if binding_error else []
    seen_namespaces: set[str] = set()
    for value in active:
        namespace = str(value)
        if namespace in seen_namespaces:
            continue
        seen_namespaces.add(namespace)
        binding = bindings.get(namespace)
        entry: dict = {"namespace": namespace}
        if namespace not in available:
            entry.update({"included": False, "reason": "namespace_missing"})
        elif binding is None:
            entry.update({"included": True, "reason": "legacy_active_namespace"})
            warnings.append(
                f"namespace {namespace!r} is active without a binding and applies globally"
            )
        elif binding["scope"] == "global":
            entry.update({"included": True, "reason": "explicit_global_binding", "binding": binding})
        elif canonical_source is None:
            entry.update({"included": False, "reason": "source_identity_unavailable", "binding": binding})
        elif binding["repository"] == canonical_source:
            entry.update({"included": True, "reason": "repository_binding_match", "binding": binding})
        else:
            entry.update({"included": False, "reason": "repository_binding_mismatch", "binding": binding})
        namespaces.append(entry)
        if entry["included"]:
            effective_namespaces.append(namespace)

    effective_rules: list[dict] = []
    seen_rule_ids: set[str] = set()
    for namespace_entry in namespaces:
        if not namespace_entry["included"]:
            continue
        namespace = namespace_entry["namespace"]
        for pattern in sorted(list_patterns(root=root, namespace=namespace), key=lambda item: item["id"]):
            rule_id = str(pattern["id"])
            if rule_id in seen_rule_ids:
                continue
            seen_rule_ids.add(rule_id)
            sidecar_ids, sidecar_error = _sidecar_rule_ids(namespace, pattern, root)
            if sidecar_error and sidecar_error not in warnings:
                warnings.append(sidecar_error)
            effective_rules.append({
                "namespace": namespace,
                "rule_id": rule_id,
                "pattern": pattern,
                "reason": namespace_entry["reason"],
                "sidecar_record_ids": sidecar_ids,
            })
    return {
        "source_identity": canonical_source,
        "namespaces": namespaces,
        "effective_namespaces": effective_namespaces,
        "effective_rules": effective_rules,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Pattern <-> markdown
# ---------------------------------------------------------------------------

def pattern_id(title: str, scope: str) -> str:
    # A content-derived dedup key over (title, scope), recomputed on every load
    # rather than persisted, so the digest algorithm can change freely. SHA-256
    # keeps this off the "broken hash" security lint even though the value is
    # never a signature or credential.
    return hashlib.sha256(f"{title.strip().lower()}|{scope}".encode()).hexdigest()[:16]


def render_pattern(p: dict) -> str:
    """Render a pattern as guidance-only markdown. A learned pattern is a single
    high-level, code-agnostic review heuristic: the title + guidance are the
    whole rule. If a rule needs a symptom anecdote or a concrete example to be
    understood, the guidance is underspecified — sharpen it instead."""
    return (
        f"### {p['title']} <!-- scope:{p.get('scope', 'common')} -->"
        f" <!-- impact:{p.get('impact', 'medium')} -->"
        f" <!-- added:{p.get('added_at', '')} -->\n"
        f"{' '.join(p.get('guidance', '').split())}\n"
    )


_HDR = re.compile(
    r"###\s+(?P<title>.*?)\s*(?:<!--\s*scope:(?P<scope>\w+)\s*-->)?"
    r"\s*(?:<!--\s*impact:(?P<impact>\w+)\s*-->)?"
    r"\s*(?:<!--\s*added:(?P<added>[^>]*?)\s*-->)?\s*$"
)


def parse_patterns(md: str) -> list[dict]:
    """Parse a learned-patterns(.candidate).md file into pattern dicts (tolerant).

    Patterns are guidance-only. Any legacy ``**Symptom ...:**`` / ``**Example:**``
    lines from the old format are ignored (they start with ``**``), so existing
    files keep parsing cleanly until the next consolidation rewrites them lean."""
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
            # Accumulate every non-empty, non-metadata line as guidance (matches
            # the JS parser); legacy Symptom/Example lines (prefixed with **) are
            # skipped, so the round-trip stays lossless for multi-line guidance.
            if s and not s.startswith("**"):
                guidance_lines.append(s)
        out.append({
            "id": pattern_id(title, scope), "title": title, "scope": scope,
            "impact": m.group("impact") or "medium", "added_at": (m.group("added") or "").strip(),
            "guidance": " ".join(guidance_lines),
        })
    return out


def list_patterns(scope: str = "common", repo_identity: str | None = None,
                  root: Path | None = None, namespace: str | None = None) -> list[dict]:
    """Parsed patterns from the consolidated file for a namespace."""
    path = common_file(root, namespace)
    if not path.exists():
        return []
    return parse_patterns(path.read_text(encoding="utf-8"))


def list_patterns_for_review(
    root: Path | None = None, source_identity: dict[str, str] | None = None
) -> list[dict]:
    """Load rules eligible for a review source, preserving legacy behaviour."""
    return [item["pattern"] for item in resolve_effective_rules(
        source_identity, root=root
    )["effective_rules"]]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write_text(path, text)


def _normalize_pattern(pattern: dict) -> dict:
    p = dict(pattern)
    p.setdefault("scope", "common")
    p.setdefault("impact", "medium")
    p.setdefault("added_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    p.setdefault("seed", False)
    p["id"] = pattern_id(p.get("title", ""), p["scope"])
    return p


_CANDIDATE_HEADER = (
    "# Pending learnings (candidate) — awaiting consolidation\n\n"
    "<!-- New learnings are appended here during reviews. Trigger \"Consolidate\" "
    "to AI-merge them into learned-patterns.md, after which this file is cleared. "
    "You may edit/curate this file directly before consolidating. -->\n\n"
)


# ---------------------------------------------------------------------------
# Stage (append a new learning to the candidate) — cheap, no model call
# ---------------------------------------------------------------------------

def stage_learning(pattern: dict, source: str, root: Path | None = None,
                   namespace: str | None = None) -> dict:
    """Append one new learning to the candidate file. Admissible-sources only
    (no self-poisoning). Deterministic; the AI merge happens later in
    ``consolidate_apply``."""
    if source not in ADMISSIBLE_SOURCES:
        raise ValueError(
            f"inadmissible learning source {source!r}; allowed: {sorted(ADMISSIBLE_SOURCES)} "
            "(the reviewer never learns from its own unpublished findings)")
    store.ensure_layout(root)
    ns_dir = _namespace_dir(namespace, root)
    ns_dir.mkdir(parents=True, exist_ok=True)
    p = _normalize_pattern(pattern)
    cf = candidate_file(root, namespace)
    # Read and write under one lock: see _candidate_lock.
    with _candidate_lock(root, namespace):
        # ONE guarded read, not two `read_text` calls. The candidate file sits in a
        # directory review workers can reach, so a prompt-injected worker can replace
        # it with a symlink to `~/.aws/credentials`; the no-link reader refuses that
        # instead of dereferencing it into the catalog. Reading once also removes the
        # window where the file changes between the emptiness test and the append.
        existing = (store.read_text_nolink(cf, ns_dir) or "") if cf.exists() else ""
        if existing.strip():
            body = (existing.rstrip() + "\n\n"
                    + render_pattern(p) + "\n")
        else:
            body = _CANDIDATE_HEADER + render_pattern(p) + "\n"
        _atomic_write(cf, body)
    return {"ok": True, "path": str(cf), "source": source,
            "namespace": namespace or DEFAULT_NAMESPACE,
            "staged": len(parse_patterns(body))}


def list_candidate(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    cf = candidate_file(root, namespace)
    if not cf.exists():
        return []
    # The dashboard renders what this returns, so an unguarded read would make a
    # planted symlink an egress path, not just a corrupted catalog.
    return parse_patterns(
        store.read_text_nolink(cf, _namespace_dir(namespace, root)) or "")


def candidate_count(root: Path | None = None, namespace: str | None = None) -> int:
    return len(list_candidate(root, namespace))


def clear_candidate(root: Path | None = None, namespace: str | None = None,
                    only_ids: Sequence[str] | None = None) -> bool:
    """Clear staged candidates.

    With ``only_ids``, keep every entry the snapshot did not account for instead of
    deleting the file. Consolidation needs this: the merge worker reads the
    candidates at dispatch and the apply lands minutes later, so a review that
    stages a learning in between would have its entry deleted by a blanket unlink
    even though the merge never saw it. Clearing exactly what was consolidated
    leaves the newer staging intact, which is cheaper and less disruptive than
    serialising every review behind the merge.

    ``only_ids`` is a MULTISET, not a set: pass one element per snapshotted entry.
    Ids are a content hash of title|scope, and staging appends without deduping, so
    duplicates are expected and the count is what distinguishes "the two entries
    the merge saw" from "a third staged behind its back".

    With ``only_ids=None`` the whole file goes, which is what an explicit
    "discard the staged learnings" action means.
    """
    cf = candidate_file(root, namespace)
    if not cf.exists():
        return False
    # Both branches run under the lock. The full unlink used to sit outside it,
    # so a `stage_learning` append could complete between the exists() check and
    # the unlink and be deleted without ever being read — the same read-modify-
    # write race the selective branch takes the lock for.
    with _candidate_lock(root, namespace):
        if not cf.exists():          # a concurrent clear got there first
            return False
        if only_ids is None:
            cf.unlink()
            return True
        # Remove at most as many entries per id as the snapshot held, oldest
        # first. Ids are a content hash of title|scope (`pattern_id`) and
        # `add_candidate` appends without deduping, so the SAME id can legitimately
        # appear more than once — a later review re-learning the same lesson for the
        # same scope produces a second entry. A set-membership filter deleted every
        # occurrence, including one staged after the snapshot that the merge never
        # saw, which is the loss this whole `only_ids` path exists to prevent.
        # Entries are appended, so consuming the budget in file order keeps the
        # newest duplicate.
        budget: collections.Counter[str] = collections.Counter(
            str(i) for i in only_ids)
        kept = []
        # Same guard as the staging read: a worker can swap this catalog for a
        # symlink, and re-serializing the target would publish it to the
        # dashboard-readable candidate file.
        for p in parse_patterns(
                store.read_text_nolink(cf, _namespace_dir(namespace, root)) or ""):
            # An entry with no id has no budget entry, so it is kept — the safe
            # direction when the snapshot cannot account for it.
            pid = str(p.get("id") or "")
            if budget.get(pid, 0) > 0:
                budget[pid] -= 1
                continue
            kept.append(p)
        if not kept:
            cf.unlink()
            return True
        _atomic_write(cf, _CANDIDATE_HEADER
                      + "\n".join(render_pattern(p) for p in kept) + "\n")
    return True


# ---------------------------------------------------------------------------
# Consolidate (apply the AI-merged result) — replaces learned-patterns.md
# ---------------------------------------------------------------------------

def _record_consolidation(consolidated: int, namespace: str | None = None,
                          root: Path | None = None) -> None:
    ns = namespace or DEFAULT_NAMESPACE
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "consolidated": consolidated, "namespace": ns}
    path = _consolidations_log(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# Optional Kiro Crew redaction, mirroring pipeline.py: present in the runtime,
# absent when the app is driven standalone outside it.
try:
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls
except ImportError:  # pragma: no cover - standalone fallback
    redact_credentials = redact_exfiltration_urls = None  # type: ignore


def _redact(text: str) -> str:
    """Scrub credentials + exfiltration URLs from worker-authored text."""
    if redact_exfiltration_urls is None or redact_credentials is None:
        return text
    return redact_credentials(redact_exfiltration_urls(text)[0])[0]


def consolidate_apply(merged_md: str, root: Path | None = None,
                      namespace: str | None = None,
                      candidate_ids: Sequence[str] | None = None) -> dict:
    """Atomically replace learned-patterns.md with the AI-merged content, then
    clear the candidate. Refuses to write empty content (never wipes the ruleset
    on a bad merge).

    The content is redacted here rather than at the caller because THIS is the
    persistence chokepoint. ``merged_md`` is written by the merge worker, which
    has shell and file tools, so it is LLM-influenced text that has read the
    reviewed diffs; a credential picked up there would otherwise land in
    learned-patterns.md, which is rendered in the dashboard AND injected into
    every later review prompt. The caller already hardens the read PATH
    (O_NOFOLLOW, inode validation, size cap) — that protects against reading the
    wrong file, not against the content of the right one.
    """
    if not merged_md or not merged_md.strip():
        return {"ok": False, "error": "merged content is empty; refusing to overwrite learned-patterns.md"}
    if not parse_patterns(merged_md):
        # Non-empty prose is not a ruleset. Writing it would replace every pattern
        # with commentary and clear the candidate file in the same call, so the
        # staged learnings would be gone with nothing to show for them.
        return {"ok": False,
                "error": "merged content has no recognizable patterns; "
                         "refusing to overwrite learned-patterns.md"}
    merged_md = _redact(merged_md)
    store.ensure_layout(root)
    staged = candidate_count(root, namespace)
    # Which candidates this merge is entitled to clear. The caller passes the set
    # it snapshotted BEFORE dispatching the worker; without one, snapshot now,
    # which still protects anything staged after this instant.
    if candidate_ids is None:
        candidate_ids = [p["id"] for p in list_candidate(root, namespace)]
    body = merged_md if merged_md.endswith("\n") else merged_md + "\n"
    # The guards above check the merged text's shape, not that it kept every rule, so
    # keep the pre-merge ruleset: a merge that parses and still loses lessons is
    # recoverable from this copy and from nowhere else.
    backup = _snapshot_before_apply(root, namespace)
    _atomic_write(common_file(root, namespace), body)
    cleared = clear_candidate(root, namespace, only_ids=candidate_ids)
    _record_consolidation(staged, namespace, root)
    result = {"ok": True, "path": str(common_file(root, namespace)),
              "namespace": namespace or DEFAULT_NAMESPACE,
              "consolidated_from_candidate": staged, "candidate_cleared": cleared,
              "patterns_now": len(list_patterns(root=root, namespace=namespace))}
    if backup is not None:
        result["backup"] = str(backup)
    return result


def _snapshot_before_apply(root: Path | None, namespace: str | None) -> Path | None:
    """Copy the current learned-patterns.md next to itself and return the copy's path.

    Read through the no-link guard, like every other read of this file, so a planted
    symlink cannot make the snapshot step read somewhere else. Returns None when there is
    nothing to preserve (first consolidation) or when the copy itself fails -- a failed
    backup must not block the merge, it only means this one apply has no undo, and the
    caller can see that from the absent ``backup`` key.
    """
    live = common_file(root, namespace)
    try:
        current = store.read_text_nolink(live, _namespace_dir(namespace, root)) or ""
    except (OSError, ValueError):
        return None
    # A seeded-but-empty catalog holds no rules, so there is nothing a merge could lose.
    # Snapshot exactly when rules exist, so the backup's presence means "there was a
    # ruleset here" rather than "the file existed".
    if not parse_patterns(current):
        return None
    dest = live.with_name(live.name + ".pre-consolidation")
    try:
        _atomic_write(dest, current if current.endswith("\n") else current + "\n")
    except OSError:
        return None
    return dest


# ---------------------------------------------------------------------------
# Seed set — deliberately MINIMAL (bootstraps the common warm-start layer)
# ---------------------------------------------------------------------------

DEFAULT_SEED_PATTERNS: list[dict] = [
    {"title": "Reset guard flags on every exit path", "scope": "common", "impact": "high",
     "dimension": "correctness", "seed": True,
     "guidance": "When a boolean guard gates a loop or state machine, ensure it is reset on ALL exit paths, including early returns and exceptions, so the next cycle never reads a stale invariant."},
    {"title": "Authorize by confirming the owner, not by rejecting known-bad", "scope": "common", "impact": "high",
     "dimension": "security", "seed": True,
     "guidance": "Authorization must positively confirm the authenticated principal. Negative-only checks (reject known-bad, reject if disabled) are fail-open — any unanticipated caller passes."},
]


def seed_common(root: Path | None = None, force: bool = False) -> int:
    """Populate the common layer with the seed patterns if it has none yet."""
    store.ensure_layout(root)
    if not force and list_patterns("common", root=root):
        return 0
    header = "# Common learned patterns (cross-repo, warm start)\n\n"
    body = header + "\n".join(render_pattern(p) for p in DEFAULT_SEED_PATTERNS)
    _atomic_write(common_file(root), body)
    return len(DEFAULT_SEED_PATTERNS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code Review Sage learning store (V2, file-centric + namespaces)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed").add_argument("--force", action="store_true")
    lp = sub.add_parser("list-patterns")
    lp.add_argument("--namespace", default=None, help="namespace to list (default: 'default')")
    lc = sub.add_parser("list-candidate")
    lc.add_argument("--namespace", default=None)
    sp = sub.add_parser("stage", help="append a new learning to the candidate file")
    sp.add_argument("--file", required=True, help="JSON pattern file")
    sp.add_argument("--source", required=True, choices=sorted(ADMISSIBLE_SOURCES))
    sp.add_argument("--namespace", default=None)
    cp = sub.add_parser("consolidate", help="apply the AI-merged learned-patterns.md and clear the candidate")
    cp.add_argument("--merged-file", required=True, help="file holding the AI-merged learned-patterns.md")
    cp.add_argument("--namespace", default=None)
    cc = sub.add_parser("clear-candidate")
    cc.add_argument("--namespace", default=None)
    er = sub.add_parser(
        "export-records", help="explicitly export legacy markdown into the record sidecar"
    )
    er.add_argument("--namespace", default=None)
    rr = sub.add_parser("rollback-records-export", help="restore the pre-export record sidecar")
    rr.add_argument("--namespace", default=None)
    sub.add_parser("list-namespaces")
    cn = sub.add_parser("create-namespace")
    cn.add_argument("name")
    dn = sub.add_parser("delete-namespace")
    dn.add_argument("name")
    sub.add_parser("list-for-review", help="patterns from all active namespaces (union)")

    args = ap.parse_args(argv)
    if args.cmd == "seed":
        print(json.dumps({"seeded": seed_common(force=args.force)}))
    elif args.cmd == "list-patterns":
        print(json.dumps(list_patterns(namespace=args.namespace), indent=2))
    elif args.cmd == "list-candidate":
        print(json.dumps(list_candidate(namespace=args.namespace), indent=2))
    elif args.cmd == "stage":
        pat = json.loads(Path(args.file).read_text(encoding="utf-8"))
        print(json.dumps(stage_learning(pat, args.source, namespace=args.namespace), indent=2))
    elif args.cmd == "consolidate":
        merged = Path(args.merged_file).read_text(encoding="utf-8")
        print(json.dumps(consolidate_apply(merged, namespace=args.namespace), indent=2))
    elif args.cmd == "clear-candidate":
        print(json.dumps({"cleared": clear_candidate(namespace=args.namespace)}))
    elif args.cmd == "export-records":
        print(json.dumps(export_learning_records(namespace=args.namespace), indent=2))
    elif args.cmd == "rollback-records-export":
        print(json.dumps(rollback_learning_records_export(namespace=args.namespace), indent=2))
    elif args.cmd == "list-namespaces":
        print(json.dumps(list_namespaces(), indent=2))
    elif args.cmd == "create-namespace":
        print(json.dumps(create_namespace(args.name), indent=2))
    elif args.cmd == "delete-namespace":
        print(json.dumps(delete_namespace(args.name), indent=2))
    elif args.cmd == "list-for-review":
        print(json.dumps(list_patterns_for_review(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
