#!/usr/bin/env python3
"""Learning system V2 — file-centric with namespace support.

Learning is **detection-gap (miss) analysis**: issues that shipped, traced back
to the introducing change, turned into a forward-looking pattern. The *judgment*
(why was it missed? which dimension was blind? how to merge it cleanly into the
ruleset?) is the LLM's job in the `learn-from-sage` skill. This module is the
deterministic backbone for the file-centric flow:

- pattern <-> markdown (the on-disk pattern format),
- ``stage_learning`` — cheap staging of a new learning in the **candidate** file
  (``learned-patterns.candidate.md``); an exact equivalent bumps a ``seen``
  count instead of appending; no model call, admissible-sources only,
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
import hashlib  # noqa: F401 — preserve the facade surface
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:  # allow `python3 sage_lib/learning.py` (run as script)
    sys.path.insert(0, _APP_ROOT)
if __name__ == "__main__":
    __package__ = "sage_lib"

from sage_lib import store  # noqa: E402

from kiro_crew import platform_compat  # noqa: E402
from kiro_crew.slugs import refused_slug_char  # noqa: E402

# Learning is mined ONLY from human-validated, ground-truth signals.
ADMISSIBLE_SOURCES = {"fix_introduce", "human_comment", "design_outcome", "import"}


CONSOLIDATION_PREVIEW_SCHEMA = "code-review-sage-consolidation-preview"
CONSOLIDATION_PREVIEW_VERSION = 1
CONSOLIDATION_PREVIEW_TTL_SECONDS = 15 * 60
_LIFECYCLE_TRANSITIONS = {
    "candidate": frozenset({"active", "archived", "pinned"}),
    "active": frozenset({"archived", "pinned"}),
    "archived": frozenset({"candidate", "active", "pinned"}),
    "pinned": frozenset({"active", "archived"}),
}


# ---------------------------------------------------------------------------
# Paths — namespace-aware
# ---------------------------------------------------------------------------


from .learning_format import (  # noqa: E402,F401 — preserve the facade surface
    _ACTIVE_CONTEXT_BUDGETS,
    _BINDING_SCOPES,
    _CANDIDATE_HEADER,
    _CONSOLIDATION_ACTIONS,
    _CONSOLIDATION_REASON_CODE_RE,
    _HDR,
    _HOST_RE,
    _LEARNING_LIFECYCLES,
    _LEGACY_META,
    _NS_NAME_RE,
    _PROMOTION_RECURRENCE_COUNT,
    _REPOSITORY_SEGMENT_RE,
    _UNKNOWN_PROVENANCE,
    DEFAULT_NAMESPACE,
    LEARNING_RECORDS_SCHEMA,
    LEARNING_RECORDS_VERSION,
    LearningRecordError,
    _atomic_write,
    _below_promotion_floor,
    _candidate_body_after_consuming,
    _candidate_patterns_with_occurrence_ids,
    _candidate_snapshot_entry,
    _candidate_snapshot_matches,
    _content_generation,
    _empty_learning_records_document,
    _is_valid_ns_name,
    _legacy_candidate_occurrence_id,
    _legacy_record,
    _legacy_record_id,
    _matching_candidate_record_ids,
    _namespace_dir,
    _normalize_pattern,
    _preview_path,
    _rank_context_records,
    _read_current_text,
    _read_learning_records_document,
    _read_markdown_patterns,
    _record_context_scope,
    _record_error,
    _record_recency,
    _record_token_cost,
    _sha256_json,
    _utc_now,
    _validate_consolidation_proposal,
    _validate_learning_record,
    _validate_recurrence_evidence,
    canonical_repository_source,
    consolidation_preview_dir,
    equivalence_key,
    parse_patterns,
    pattern_id,
    render_pattern,
    select_active_context,
    validate_active_context_budgets,
    validate_learning_records_document,
    validate_namespace_bindings,
)


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


def load_learning_records(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    """Read durable records without migrating or otherwise changing disk state."""
    document, _raw = _read_learning_records_document(
        learning_records_file(root, namespace), _namespace_dir(namespace, root)
    )
    return list(document["records"])


def _write_learning_records(document: dict, root: Path | None, namespace: str | None) -> None:
    validate_learning_records_document(document)
    _atomic_write(
        learning_records_file(root, namespace),
        json.dumps(document, indent=2, sort_keys=True) + "\n",
    )


def record_recurrence_evidence(
    record_id: str, evidence: object, *, root: Path | None = None, namespace: str | None = None
) -> dict:
    """Persist one independent recurrence signal; duplicate reviews or changes do not count."""
    _validate_recurrence_evidence([evidence], 1)
    assert isinstance(evidence, dict)
    with _candidate_lock(root, namespace):
        path = learning_records_file(root, namespace)
        document, _raw = _read_learning_records_document(path, _namespace_dir(namespace, root))
        records = [dict(item) for item in document["records"]]
        for index, record in enumerate(records):
            if record["id"] != record_id:
                continue
            existing = record["recurrence"]["evidence"]
            if any(
                item["review"] == evidence["review"] or item["change"] == evidence["change"]
                for item in existing
            ):
                return {
                    "ok": True,
                    "changed": False,
                    "reason": "duplicate_recurrence_evidence",
                    "record": record,
                }
            recurrence = {"count": len(existing) + 1, "evidence": [*existing, dict(evidence)]}
            updated = dict(record)
            updated["recurrence"] = recurrence
            updated["timestamps"] = {**record["timestamps"], "updated_at": _utc_now()}
            records[index] = updated
            document = {**document, "records": records}
            _write_learning_records(document, root, namespace)
            return {"ok": True, "changed": True, "record": updated}
    raise KeyError(f"learning record {record_id!r} does not exist")


def enforce_active_context_budgets(
    *, root: Path | None = None, namespace: str | None = None, config: dict | None = None
) -> dict:
    """Archive unpinned active overflow while preserving archived history and pins."""
    cfg = config if config is not None else store.load_config(root)
    review = cfg.get("review") if isinstance(cfg, dict) else None
    budgets = validate_active_context_budgets(review)
    assert isinstance(review, dict)
    bindings = validate_namespace_bindings(review.get("namespace_bindings"))
    binding = bindings.get(namespace or DEFAULT_NAMESPACE, {})
    default_scope = "repository" if binding.get("scope") == "repository" else "global"
    with _candidate_lock(root, namespace):
        path = learning_records_file(root, namespace)
        document, _raw = _read_learning_records_document(path, _namespace_dir(namespace, root))
        candidates = [
            {
                "record": record,
                "namespace": namespace or DEFAULT_NAMESPACE,
                "scope": _record_context_scope(record, default_scope),
            }
            for record in document["records"]
            if record["lifecycle"] in {"active", "pinned"}
        ]
        selection = select_active_context(candidates, budgets)
        overflow = {
            decision["record_id"]
            for decision in selection["decisions"]
            if decision["reason"] != "selected" and decision["lifecycle"] == "active"
        }
        if not overflow:
            return {"ok": True, "archived": [], **selection}
        now = _utc_now()
        updated_records = []
        for record in document["records"]:
            if record["id"] not in overflow:
                updated_records.append(record)
                continue
            updated = dict(record)
            updated["lifecycle"] = "archived"
            updated["timestamps"] = {**record["timestamps"], "updated_at": now, "archived_at": now}
            updated_records.append(updated)
        updated_document = {**document, "records": updated_records}
        _write_learning_records(updated_document, root, namespace)
        return {"ok": True, "archived": sorted(overflow), **selection}


def transition_learning_record(
    record_id: str,
    lifecycle: str,
    *,
    operator: str | None = None,
    root: Path | None = None,
    namespace: str | None = None,
    config: dict | None = None,
) -> dict:
    """Apply one explicit lifecycle transition and enforce active-context capacity."""
    if lifecycle not in _LEARNING_LIFECYCLES:
        raise ValueError(f"unknown lifecycle {lifecycle!r}")
    transitioned = False
    with _candidate_lock(root, namespace):
        path = learning_records_file(root, namespace)
        document, _raw = _read_learning_records_document(path, _namespace_dir(namespace, root))
        records = [dict(item) for item in document["records"]]
        for index, record in enumerate(records):
            if record["id"] != record_id:
                continue
            current = record["lifecycle"]
            if lifecycle not in _LIFECYCLE_TRANSITIONS[current]:
                raise ValueError(f"invalid lifecycle transition {current!r} -> {lifecycle!r}")
            if (current == "pinned" or lifecycle == "pinned") and not (operator or "").strip():
                raise ValueError("operator identity is required for a pin or unpin transition")
            if (
                current == "candidate"
                and lifecycle == "active"
                and (record["recurrence"]["count"] < _PROMOTION_RECURRENCE_COUNT)
            ):
                raise ValueError(
                    "candidate promotion requires independent recurrence evidence or an operator pin"
                )
            now = _utc_now()
            updated = dict(record)
            updated["lifecycle"] = lifecycle
            # Keep enough durable state for the explicit restore action to
            # return this rule to its pre-archive lifecycle without deleting
            # its origin or recurrence evidence.
            if lifecycle == "archived" and current in {"active", "pinned"}:
                updated["archived_from"] = current
            updated["timestamps"] = {
                **record["timestamps"],
                "updated_at": now,
                "archived_at": now if lifecycle == "archived" else None,
            }
            records[index] = updated
            _write_learning_records({**document, "records": records}, root, namespace)
            transitioned = True
            break
    if not transitioned:
        raise KeyError(f"learning record {record_id!r} does not exist")
    capacity = enforce_active_context_budgets(root=root, namespace=namespace, config=config)
    final = next(item for item in load_learning_records(root, namespace) if item["id"] == record_id)
    return {"ok": True, "record": final, "capacity": capacity}


def archive_learning_record(
    record_id: str,
    *,
    operator: str,
    root: Path | None = None,
    namespace: str | None = None,
    config: dict | None = None,
) -> dict:
    """Archive one loaded rule without deleting its governed record."""
    record = next(
        (item for item in load_learning_records(root, namespace) if item["id"] == record_id),
        None,
    )
    if record is None:
        raise KeyError(f"learning record {record_id!r} does not exist")
    if record["lifecycle"] not in {"active", "pinned"}:
        raise ValueError("only active or pinned learning records can be archived")
    return transition_learning_record(
        record_id, "archived", operator=operator, root=root, namespace=namespace, config=config
    )


def restore_learning_record(
    record_id: str,
    *,
    operator: str,
    root: Path | None = None,
    namespace: str | None = None,
    config: dict | None = None,
) -> dict:
    """Restore an archived rule to the lifecycle it held before archiving."""
    record = next(
        (item for item in load_learning_records(root, namespace) if item["id"] == record_id),
        None,
    )
    if record is None:
        raise KeyError(f"learning record {record_id!r} does not exist")
    if record["lifecycle"] != "archived":
        raise ValueError("only archived learning records can be restored")
    return transition_learning_record(
        record_id,
        record.get("archived_from") or "active",
        operator=operator,
        root=root,
        namespace=namespace,
        config=config,
    )


def export_learning_records(root: Path | None = None, namespace: str | None = None) -> dict:
    """Add legacy markdown entries to the versioned sidecar without touching markdown.

    This is an explicit migration/export operation. It never runs on import or a
    normal read, retains valid existing records, and snapshots the old sidecar
    before publishing a merged replacement.
    """
    ns = namespace or DEFAULT_NAMESPACE
    with _candidate_lock(root, namespace):
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


def list_rule_entries(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    """List rule rows while keeping legacy markdown migration action-triggered."""
    ns = namespace or DEFAULT_NAMESPACE
    records_path = learning_records_file(root, ns)
    records = load_learning_records(root, ns) if records_path.exists() else []
    if records:
        return [
            {
                "id": record["id"],
                "record_id": record["id"],
                "title": str(record["text"]).splitlines()[0],
                "scope": record["scope"],
                "impact": "medium",
                "added_at": record["timestamps"].get("created_at") or "",
                "guidance": record["rule"],
                "lifecycle": record["lifecycle"],
                "legacy": record["legacy"],
            }
            for record in records
            if record["lifecycle"] != "candidate"
        ]

    occurrences: collections.Counter[str] = collections.Counter()
    entries = []
    for pattern in list_patterns(root=root, namespace=ns):
        identity = _legacy_record_id(ns, "active", 0, pattern)
        occurrences[identity] += 1
        entries.append(
            {
                **pattern,
                "record_id": _legacy_record_id(ns, "active", occurrences[identity], pattern),
                "lifecycle": "active",
                "legacy": True,
            }
        )
    return entries


def rollback_learning_records_export(
    root: Path | None = None, namespace: str | None = None
) -> dict:
    """Restore the validated pre-export sidecar without changing markdown files."""
    with _candidate_lock(root, namespace):
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
    store.mkdir_refusing_links(ns_dir)
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
                raise OSError(f"refusing to lock {lock_path}: not a lone regular file")
            with platform_compat.file_lock(fd, exclusive=True):
                _recover_consolidation_preview_transaction(root, namespace)
                yield
        finally:
            os.close(fd)


_PREVIEW_TRANSACTION_SCHEMA = "code-review-sage-consolidation-apply"
_PREVIEW_TRANSACTION_VERSION = 1
_PREVIEW_TRANSACTION_FILES = ("live", "sidecar", "candidate", "preview")


def _preview_transaction_path(root: Path | None, namespace: str | None) -> Path:
    return _namespace_dir(namespace, root) / "consolidation-apply.v1.json"


def _preview_transaction_paths(
    preview_id: str, root: Path | None, namespace: str | None
) -> dict[str, Path]:
    ns = namespace or DEFAULT_NAMESPACE
    return {
        "live": common_file(root, ns),
        "sidecar": learning_records_file(root, ns),
        "candidate": candidate_file(root, ns),
        "preview": _preview_path(preview_id, root, ns),
    }


def _read_preview_transaction(root: Path | None, namespace: str | None) -> dict | None:
    path = _preview_transaction_path(root, namespace)
    if not path.exists():
        return None
    try:
        value = json.loads(store.read_text_nolink(path, _namespace_dir(namespace, root)) or "")
    except (OSError, ValueError) as exc:
        raise LearningRecordError("invalid consolidation apply journal") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != _PREVIEW_TRANSACTION_SCHEMA
        or value.get("version") != _PREVIEW_TRANSACTION_VERSION
        or value.get("status") not in {"prepared", "committed"}
        or not isinstance(value.get("preview_id"), str)
        or set(value.get("before") or {}) != set(_PREVIEW_TRANSACTION_FILES)
        or set(value.get("after") or {}) != set(_PREVIEW_TRANSACTION_FILES)
        or any(
            raw is not None and not isinstance(raw, str)
            for snapshot in (value["before"], value["after"])
            for raw in snapshot.values()
        )
    ):
        raise LearningRecordError("invalid consolidation apply journal")
    return value


def _write_preview_transaction(root: Path | None, namespace: str | None, transaction: dict) -> None:
    _atomic_write(
        _preview_transaction_path(root, namespace),
        json.dumps(transaction, indent=2, sort_keys=True) + "\n",
    )


def _write_preview_snapshot(paths: dict[str, Path], snapshot: dict[str, str | None]) -> None:
    for name in _PREVIEW_TRANSACTION_FILES:
        path = paths[name]
        raw = snapshot[name]
        if raw is None:
            if path.exists():
                path.unlink()
        elif _read_current_text(path, path.parent) != raw:
            _atomic_write(path, raw)


def _recover_consolidation_preview_transaction(root: Path | None, namespace: str | None) -> None:
    """Finish or undo an interrupted preview apply before exposing namespace state."""
    transaction = _read_preview_transaction(root, namespace)
    if transaction is None:
        return
    paths = _preview_transaction_paths(transaction["preview_id"], root, namespace)
    snapshot = (
        transaction["after"] if transaction["status"] == "committed" else transaction["before"]
    )
    _write_preview_snapshot(paths, snapshot)
    _preview_transaction_path(root, namespace).unlink(missing_ok=True)


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
    typed = name
    name = name.strip().lower().replace(" ", "-")
    if not _is_valid_ns_name(name):
        # `code` + `char` are what the dashboard renders, in the user's language;
        # the English `error` is for logs and catalog-less callers. `char` comes
        # from what the user typed, before lowercasing, so it is a character they
        # will recognize -- not a lowercase stand-in that can look like an
        # allowed Latin letter.
        return {
            "ok": False,
            "code": "invalid_namespace_name",
            "char": refused_slug_char(typed),
            "error": (
                f"invalid namespace name: {name!r} (use lowercase a-z, 0-9, '.', '_' "
                "or '-', starting and ending with a letter or digit, 2-64 chars)"
            ),
        }
    if name == DEFAULT_NAMESPACE:
        return {"ok": False, "error": "'default' namespace already exists (it maps to common/)"}
    ns_path = _namespace_dir(name, root)
    if (ns_path / "learned-patterns.md").exists():
        return {"ok": False, "error": f"namespace {name!r} already exists"}
    store.mkdir_refusing_links(ns_path)
    header = f"# Learned patterns — namespace: {name}\n\n"
    store.atomic_write_text(ns_path / "learned-patterns.md", header)
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


def _sidecar_rule_ids(
    namespace: str, pattern: dict, root: Path | None
) -> tuple[list[str], str | None]:
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
    try:
        active_context_budgets = validate_active_context_budgets(review)
    except ValueError as exc:
        return {
            "source_identity": None,
            "namespaces": [],
            "effective_namespaces": [],
            "effective_rules": [],
            "active_context": {"fail_closed": True, "error": str(exc)},
            "warnings": [str(exc)],
        }
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
            entry.update(
                {"included": True, "reason": "explicit_global_binding", "binding": binding}
            )
        elif canonical_source is None:
            entry.update(
                {"included": False, "reason": "source_identity_unavailable", "binding": binding}
            )
        elif binding["repository"] == canonical_source:
            entry.update(
                {"included": True, "reason": "repository_binding_match", "binding": binding}
            )
        else:
            entry.update(
                {"included": False, "reason": "repository_binding_mismatch", "binding": binding}
            )
        namespaces.append(entry)
        if entry["included"]:
            effective_namespaces.append(namespace)

    effective_rules: list[dict] = []
    seen_rule_ids: set[str] = set()
    sidecar_candidates: list[dict] = []
    legacy_rules: list[dict] = []
    for namespace_entry in namespaces:
        if not namespace_entry["included"]:
            continue
        namespace = namespace_entry["namespace"]
        records_path = learning_records_file(root, namespace)
        try:
            records = load_learning_records(root, namespace) if records_path.exists() else []
        except LearningRecordError as exc:
            namespace_entry["sidecar"] = "invalid"
            namespace_entry["sidecar_error"] = str(exc)
            warnings.append(str(exc))
            continue
        if records:
            namespace_entry["sidecar"] = "governing"
            context_scope = (
                "repository"
                if namespace_entry.get("binding", {}).get("scope") == "repository"
                else "global"
            )
            for record in records:
                if record["lifecycle"] not in {"active", "pinned"}:
                    namespace_entry.setdefault("record_decisions", []).append(
                        {
                            "record_id": record["id"],
                            "lifecycle": record["lifecycle"],
                            "reason": "excluded_lifecycle_" + record["lifecycle"],
                        }
                    )
                    continue
                sidecar_candidates.append(
                    {
                        "record": record,
                        "namespace": namespace,
                        "scope": _record_context_scope(record, context_scope),
                        "namespace_reason": namespace_entry["reason"],
                    }
                )
            continue
        namespace_entry["sidecar"] = "legacy_markdown"
        for pattern in sorted(
            list_patterns(root=root, namespace=namespace), key=lambda item: item["id"]
        ):
            legacy_rules.append(
                {
                    "namespace": namespace,
                    "rule_id": str(pattern["id"]),
                    "pattern": pattern,
                    "reason": namespace_entry["reason"],
                    "sidecar_record_ids": [],
                    "context_reason": "legacy_markdown_compatibility",
                }
            )

    active_context = select_active_context(sidecar_candidates, active_context_budgets)
    for decision in active_context["decisions"]:
        for namespace_entry in namespaces:
            if namespace_entry["namespace"] == decision["namespace"]:
                namespace_entry.setdefault("record_decisions", []).append(decision)
                break
    for candidate in active_context["selected"]:
        record = candidate["record"]
        pattern = {
            "id": record["id"],
            "title": str(record["text"]).splitlines()[0],
            "scope": record["scope"],
            "impact": "medium",
            "added_at": record["timestamps"].get("created_at") or "",
            "guidance": record["rule"],
        }
        rule_id = pattern_id(pattern["title"], pattern["scope"])
        if rule_id in seen_rule_ids:
            continue
        seen_rule_ids.add(rule_id)
        effective_rules.append(
            {
                "namespace": candidate["namespace"],
                "rule_id": rule_id,
                "pattern": pattern,
                "reason": candidate["namespace_reason"],
                "sidecar_record_ids": [record["id"]],
                "context_reason": "bounded_sidecar_record",
                "lifecycle": record["lifecycle"],
                "recurrence_count": record["recurrence"]["count"],
                "token_cost": _record_token_cost(record),
            }
        )
    for legacy_rule in legacy_rules:
        rule_id = legacy_rule["rule_id"]
        if rule_id in seen_rule_ids:
            continue
        seen_rule_ids.add(rule_id)
        effective_rules.append(legacy_rule)
    return {
        "source_identity": canonical_source,
        "namespaces": namespaces,
        "effective_namespaces": effective_namespaces,
        "effective_rules": effective_rules,
        "active_context": active_context,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Pattern <-> markdown
# ---------------------------------------------------------------------------


# `seen` is bounded to nine digits so a crafted count can never reach int()'s
# huge-string limit; a longer one fails the optional group like any other
# unrecognised comment.
_SEEN_MAX = 999_999_999
_ISO_STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)


def list_patterns(
    scope: str = "common",
    repo_identity: str | None = None,
    root: Path | None = None,
    namespace: str | None = None,
) -> list[dict]:
    """Parsed patterns from the consolidated file for a namespace."""
    path = common_file(root, namespace)
    if not path.exists():
        return []
    return parse_patterns(path.read_text(encoding="utf-8"))


def list_patterns_for_review(
    root: Path | None = None, source_identity: dict[str, str] | None = None
) -> list[dict]:
    """Load rules eligible for a review source, preserving legacy behaviour."""
    return [
        item["pattern"]
        for item in resolve_effective_rules(source_identity, root=root)["effective_rules"]
    ]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stage (append a new learning to the candidate) — cheap, no model call
# ---------------------------------------------------------------------------


def stage_learning(
    pattern: dict, source: str, root: Path | None = None, namespace: str | None = None
) -> dict:
    """Append one new learning to the candidate file. Admissible-sources only
    (no self-poisoning). Deterministic; the AI merge happens later in
    ``consolidate_apply``.

    An exact equivalent of an entry already staged (same ``equivalence_key``)
    bumps that entry's ``seen`` count and ``last`` date instead of appending, and
    only its header line is rewritten. Anything else, including the same title
    and scope with different guidance or impact, is appended as its own entry."""
    if source not in ADMISSIBLE_SOURCES:
        raise ValueError(
            f"inadmissible learning source {source!r}; allowed: {sorted(ADMISSIBLE_SOURCES)} "
            "(the reviewer never learns from its own unpublished findings)"
        )
    store.ensure_layout(root)
    ns_dir = _namespace_dir(namespace, root)
    store.mkdir_refusing_links(ns_dir)
    p = _normalize_pattern(pattern)
    # Recurrence is the store's to count, never the caller's to claim: the pattern
    # JSON is worker-written, and a supplied count would either inflate one
    # sighting or render outside the header grammar.
    p.pop("seen", None)
    p.pop("last_seen_at", None)
    # `added_at` is rendered into the header verbatim, so a worker-written value
    # could smuggle its own `seen`/`last` comments in. Keep only a plain ISO-8601
    # date or timestamp; anything else is replaced by the staging time.
    if not _ISO_STAMP.fullmatch(str(p.get("added_at") or "")):
        p["added_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # One learning must stage as exactly one entry. A title or guidance that
    # renders its own `### ` heading would land as a second block whose header
    # (and `seen` count) the worker wrote, so it is refused, not stored.
    # Title, scope and impact are written into the header line verbatim, so any
    # line break there (`\r`, `\u2028` and friends, not only `\n`) would start a
    # line that a text-mode reader takes as a heading of its own.
    for field in ("title", "scope", "impact"):
        value = str(p.get(field) or "")
        if "".join(value.splitlines()) != value:
            raise ValueError(f"a learning's {field} must be a single line")
    staged = parse_patterns(render_pattern(p))
    if len(staged) != 1:
        raise ValueError(
            "a learning must stage as exactly one pattern; its title or guidance "
            "starts a new '### ' heading"
        )
    # A fresh learning is one sighting with no later date. Anything else means
    # caller text reached the header's own comments, so refuse rather than store
    # a count the store did not make.
    if staged[0]["seen"] != 1 or staged[0]["last_seen_at"]:
        raise ValueError("a learning's text must not carry seen/last comments")
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
            body = existing.rstrip() + "\n\n" + render_pattern(p) + "\n"
        else:
            body = _CANDIDATE_HEADER + render_pattern(p) + "\n"
        records_path = learning_records_file(root, namespace)
        records, _raw = _read_learning_records_document(records_path, ns_dir)
        coalesced = False
        if not records["records"] and existing.strip():
            combined = _coalesce(existing, p)
            if combined is not None:
                body = combined
                coalesced = True
        if records["records"]:
            # A governed namespace must never fall back to content-derived
            # candidate identity: identical discoveries are separate lifecycle
            # occurrences with distinct provenance and operator actions.
            occurrence_id = str(uuid.uuid4())
            p["occurrence_id"] = occurrence_id
            rendered = render_pattern(p)
            body = (
                existing.rstrip() + "\n\n" + rendered + "\n"
                if existing.strip()
                else _CANDIDATE_HEADER + rendered + "\n"
            )
            now = _utc_now()
            record = {
                "id": occurrence_id,
                "text": "\n".join(
                    part
                    for part in (str(p.get("title") or ""), str(p.get("guidance") or ""))
                    if part
                ),
                "rule": str(p.get("guidance") or "") or str(p.get("title") or ""),
                "namespace": namespace or DEFAULT_NAMESPACE,
                "scope": str(p.get("scope") or "common"),
                "lifecycle": "candidate",
                "origin": {"source": source, "reference": None},
                "repository_identity": None,
                "timestamps": {"created_at": now, "updated_at": now, "archived_at": None},
                "recurrence": {"count": 1, "evidence": []},
                "legacy": False,
            }
            _write_learning_records(
                {**records, "records": [*records["records"], record]}, root, namespace
            )
        _atomic_write(cf, body)
    return {
        "ok": True,
        "path": str(cf),
        "source": source,
        "namespace": namespace or DEFAULT_NAMESPACE,
        "staged": len(parse_patterns(body)),
        "coalesced": coalesced,
    }


def _list_candidate_unlocked(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    cf = candidate_file(root, namespace)
    if not cf.exists():
        return []
    # The dashboard renders what this returns, so an unguarded read would make a
    # planted symlink an egress path, not just a corrupted catalog.
    return _candidate_patterns_with_occurrence_ids(
        parse_patterns(store.read_text_nolink(cf, _namespace_dir(namespace, root)) or ""),
        namespace or DEFAULT_NAMESPACE,
    )


def list_candidate(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    with _candidate_lock(root, namespace):
        return _list_candidate_unlocked(root, namespace)


def _coalesce(existing: str, p: dict) -> str | None:
    """``existing`` with the first exact equivalent of ``p`` counted once more.

    Returns None when nothing is equivalent. The new pattern's key is taken from
    what it would look like ON DISK (rendered, then parsed), so the comparison
    sees the same normalisation a later load applies. Only the matching block's
    header line changes; every other byte, including hand curation, is kept."""
    rendered = parse_patterns(render_pattern(p))
    # Coalesce only what survives the round trip intact. A count bump stores
    # nothing of the new learning, so if parsing would lose or reshape any of
    # its text (a title or impact the header grammar cannot hold), appending is
    # the only choice that loses nothing. `stage_learning` has already refused a
    # learning that renders as more than one block.
    if len(rendered) != 1:
        return None
    new = rendered[0]
    if (
        new["title"] != str(p.get("title") or "").strip()
        or new["guidance"] != " ".join(str(p.get("guidance") or "").split())
        or new["scope"] != p["scope"]
        or new["impact"] != p["impact"]
    ):
        return None
    blocks = re.split(r"^(?=### )", existing, flags=re.M)
    for i, block in enumerate(blocks):
        parsed = parse_patterns(block) if block.startswith("### ") else []
        if not parsed or parsed[0]["key"] != new["key"]:
            continue
        hit = parsed[0]
        # Saturate at the header grammar's nine-digit bound so the count can
        # never render as a comment the parser folds into the title.
        hit["seen"] = min(hit["seen"] + 1, _SEEN_MAX)
        hit["last_seen_at"] = new["added_at"]
        # Split on the same line boundaries the parser used (`splitlines`), so a
        # hand-edited block with CR or other line endings keeps its body intact.
        first, *rest = block.splitlines(keepends=True)
        ending = first[len(first.splitlines()[0]) :]
        blocks[i] = render_pattern(hit).split("\n", 1)[0] + ending + "".join(rest)
        return "".join(blocks)
    return None


def candidate_snapshot(root: Path | None = None, namespace: str | None = None) -> list[dict]:
    """What a consolidation may later clear: each entry's key and count.

    Taken before the merge worker is dispatched and handed back to
    ``clear_candidate`` (via ``consolidate_apply``), which subtracts these counts,
    so a sighting staged while the merge runs is kept."""
    return [
        {"id": p["id"], "key": p["key"], "seen": p["seen"]} for p in list_candidate(root, namespace)
    ]


def candidate_count(root: Path | None = None, namespace: str | None = None) -> int:
    return len(list_candidate(root, namespace))


def clear_candidate(
    root: Path | None = None,
    namespace: str | None = None,
    only_ids: Sequence[str | Mapping[str, object]] | None = None,
) -> bool:
    """Clear staged candidates.

    With ``only_ids``, keep every entry the snapshot did not account for instead of
    deleting the file. Consolidation needs this: the merge worker reads the
    candidates at dispatch and the apply lands minutes later, so a review that
    stages a learning in between would have its entry deleted by a blanket unlink
    even though the merge never saw it. Clearing exactly what was consolidated
    leaves the newer staging intact, which is cheaper and less disruptive than
    serialising every review behind the merge.

    ``only_ids`` is a MULTISET, not a set. Each element is either a
    ``candidate_snapshot`` entry (``{"key", "seen"}``) or a bare id:

    - A snapshot entry subtracts its ``seen`` count from the entries with that
      equivalence key. An entry keeps ``seen_now - seen_at_snapshot`` and goes
      only when that reaches zero, so a recurrence staged after dispatch survives.
    - A bare id accounts for exactly one occurrence of that identity, as before
      counts existed. Ids hash only title|scope, so the SAME id can carry several
      entries; the count is what distinguishes "the two the merge saw" from "a
      third staged behind its back".

    With ``only_ids=None`` the whole file goes, which is what an explicit
    "discard the staged learnings" action means.
    """
    cf = candidate_file(root, namespace)
    if not cf.exists():
        return False
    # Both branches run under the lock. With the full unlink outside it, a
    # `stage_learning` append can complete between the exists() check and the
    # unlink and be deleted without ever being read — the same read-modify-
    # write race the selective branch takes the lock for.
    with _candidate_lock(root, namespace):
        if not cf.exists():  # a concurrent clear got there first
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
        id_budget: collections.Counter[str] = collections.Counter()
        key_budget: collections.Counter[str] = collections.Counter()
        for item in only_ids:
            if isinstance(item, Mapping):
                key = str(item.get("key") or "")
                count = item.get("seen")
                if key and isinstance(count, int) and not isinstance(count, bool) and count > 0:
                    key_budget[key] += count
            elif isinstance(item, str):
                id_budget[item] += 1
        kept = []
        # Same guard as the staging read: a worker can swap this catalog for a
        # symlink, and re-serializing the target would publish it to the
        # dashboard-readable candidate file.
        for p in _list_candidate_unlocked(root, namespace):
            # An entry with no id has no budget entry, so it is kept — the safe
            # direction when the snapshot cannot account for it.
            pid = str(p.get("id") or "")
            seen = int(p.get("seen") or 1)
            key = str(p.get("key") or "")
            take = min(seen, key_budget[key])
            key_budget[key] -= take
            seen -= take
            if seen and id_budget[pid] > 0:
                id_budget[pid] -= 1
                seen -= 1
            if seen <= 0:
                continue
            p["seen"] = seen
            kept.append(p)
        if not kept:
            cf.unlink()
            return True
        _atomic_write(cf, _CANDIDATE_HEADER + "\n".join(render_pattern(p) for p in kept) + "\n")
    return True


# ---------------------------------------------------------------------------
# Consolidate (apply the AI-merged result) — replaces learned-patterns.md
# ---------------------------------------------------------------------------


def _record_consolidation(
    consolidated: int, namespace: str | None = None, root: Path | None = None
) -> None:
    ns = namespace or DEFAULT_NAMESPACE
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "consolidated": consolidated,
        "namespace": ns,
    }
    path = _consolidations_log(root)
    store.mkdir_refusing_links(path.parent)
    # An APPEND, not a staged replace. ``atomic_write_text`` would have to read
    # the whole log, add this line and rename a fresh file over it, and two
    # consolidations racing that sequence each publish a file missing the other's
    # entry, so a completed consolidation disappears from the log. ``O_APPEND``
    # keeps each record's write indivisible instead.
    #
    # The chain is resolved ONCE, inside the helper, and the leaf is opened
    # relative to that pinned parent. Opening the full path by name here would
    # re-resolve every ancestor after the guard above had already passed, so a
    # component swapped in between would redirect the append; and the leaf's own
    # name needs a check of its own either way, because an append FOLLOWS a link
    # at the final component and writes into whatever it points at.
    with os.fdopen(store.open_append_nolink(path), "a", encoding="utf-8") as fh:
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


def consolidate_apply(
    merged_md: str,
    root: Path | None = None,
    namespace: str | None = None,
    candidate_ids: Sequence[str | Mapping[str, object]] | None = None,
) -> dict:
    """Atomically replace learned-patterns.md with the AI-merged content, then
    clear the candidate. Refuses to write empty, unparseable or duplicate-ID
    content (never wipes the ruleset on a bad merge).

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
        return {
            "ok": False,
            "error": "merged content is empty; refusing to overwrite learned-patterns.md",
        }
    # Shape is judged on the worker's text as written: redaction is a storage
    # transformation, and scrubbing a credential out of a heading must not be what
    # decides whether the merge is a ruleset at all.
    if not parse_patterns(merged_md):
        # Non-empty prose is not a ruleset. Writing it would replace every pattern
        # with commentary and clear the candidate file in the same call, so the
        # staged learnings would be gone with nothing to show for them.
        return {
            "ok": False,
            "error": "merged content has no recognizable patterns; "
            "refusing to overwrite learned-patterns.md",
        }
    stored_md = _redact(merged_md)
    stored_patterns = parse_patterns(stored_md)
    if not stored_patterns:
        # The redacted text is what gets stored, so a scrub that leaves no
        # recognizable pattern is refused here rather than written and cleared.
        return {
            "ok": False,
            "error": "redaction left no recognizable patterns; "
            "refusing to overwrite learned-patterns.md",
        }
    # Uniqueness is judged on the STORED text, not the worker's: redaction can erase
    # the only difference between two titles (two distinct credentials both become
    # the same tag), so a pre-redaction count can pass two patterns that land as one
    # duplicate pair on disk.
    counts = collections.Counter(p["id"] for p in stored_patterns)
    duplicate_ids = sorted(pid for pid, count in counts.items() if count > 1)
    if duplicate_ids:
        # Titles, not just the content hashes: the caller's retry prompt and a human
        # both need to know WHICH rules collided before they can merge them.
        duplicate_set = set(duplicate_ids)
        duplicate_titles = sorted({p["title"] for p in stored_patterns if p["id"] in duplicate_set})
        named = "; ".join(duplicate_titles) if duplicate_titles else "see duplicate_ids"
        return {
            "ok": False,
            "duplicate_ids": duplicate_ids,
            "duplicate_titles": duplicate_titles,
            "error": "merged content has duplicate pattern IDs; merge each "
            f"repeated title and scope into one rule and retry ({named})",
        }
    store.ensure_layout(root)
    staged = candidate_count(root, namespace)
    # Which candidates this merge is entitled to clear. The caller passes the
    # `candidate_snapshot` it took BEFORE dispatching the worker; without one,
    # snapshot now, which still protects anything staged after this instant.
    if candidate_ids is None:
        candidate_ids = candidate_snapshot(root, namespace)
    body = stored_md if stored_md.endswith("\n") else stored_md + "\n"
    # The guards above check the merged text's shape, not that it kept every rule, so
    # keep the pre-merge ruleset: a merge that parses and still loses lessons is
    # recoverable from this copy and from nowhere else.
    backup = _snapshot_before_apply(root, namespace)
    _atomic_write(common_file(root, namespace), body)
    cleared = clear_candidate(root, namespace, only_ids=candidate_ids)
    _record_consolidation(staged, namespace, root)
    result = {
        "ok": True,
        "path": str(common_file(root, namespace)),
        "namespace": namespace or DEFAULT_NAMESPACE,
        "consolidated_from_candidate": staged,
        "candidate_cleared": cleared,
        "patterns_now": len(list_patterns(root=root, namespace=namespace)),
    }
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
# Curated consolidation previews
# ---------------------------------------------------------------------------


def promotion_ineligible_candidate_ids(
    snapshot: Sequence[dict],
    candidate_ids: Sequence[str],
    *,
    root: Path | None = None,
    namespace: str | None = None,
) -> list[str]:
    """Selected candidates whose promote/merge decision the sidecar gate rejects.

    The merge worker cannot see the governed sidecar, so without this list it
    proposes a promotion that raises inside the preview and surfaces only as
    "malformed_worker_output" — an unfixable error with no cause attached.
    """
    ns = namespace or DEFAULT_NAMESPACE
    document, _raw = _read_learning_records_document(
        learning_records_file(root, ns), _namespace_dir(ns, root)
    )
    records = list(document.get("records") or [])
    selected = {str(item) for item in candidate_ids}
    blocked: list[str] = []
    for entry in snapshot:
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        entry_id = str(entry.get("id") or "")
        if not isinstance(pattern, dict) or entry_id not in selected:
            continue
        # Mirrors the two loops in _preview_sidecar_document: an id match gates on
        # its own, a rule-text match only when it identifies one record.
        by_id = [record for record in records if str(record.get("id")) == str(pattern.get("id"))]
        if by_id:
            gated = by_id
        else:
            matched = _matching_candidate_record_ids(records, pattern)
            gated = (
                [record for record in records if str(record.get("id")) == matched[0]]
                if len(matched) == 1
                else []
            )
        if any(_below_promotion_floor(record) for record in gated):
            blocked.append(entry_id)
    return blocked


def _preview_sidecar_document(
    document: dict | None,
    decisions: Sequence[dict],
    snapshot: Sequence[dict],
    *,
    root: Path | None,
    namespace: str | None,
) -> tuple[dict | None, dict]:
    """Apply deterministic lifecycle effects to a copy, never the live sidecar."""
    if document is None:
        return None, {"governed": False, "archived_record_ids": [], "selection": None}
    proposed = json.loads(json.dumps(document))
    decision_by_id = {item["candidate_id"]: item for item in decisions}
    snapshot_by_id: dict[str, list[dict]] = {}
    for item in snapshot:
        snapshot_by_id.setdefault(item["id"], []).append(item)
    now = _utc_now()
    for record in proposed["records"]:
        for candidate in snapshot_by_id.get(str(record.get("id") or ""), []):
            decision = decision_by_id.get(candidate["id"])
            if decision is None:
                continue
            action = decision["action"]
            if action in {"merge", "promote"} and record["lifecycle"] == "candidate":
                if _below_promotion_floor(record):
                    raise ValueError("candidate_promotion_not_eligible")
                record["lifecycle"] = "active"
                record["timestamps"] = {
                    **record["timestamps"],
                    "updated_at": now,
                    "archived_at": None,
                }
            elif action == "archive" and record["lifecycle"] != "archived":
                record["lifecycle"] = "archived"
                record["timestamps"] = {
                    **record["timestamps"],
                    "updated_at": now,
                    "archived_at": now,
                }
    # Legacy sidecars carry different record ids. Match their rule payloads only when
    # the candidate relation is unambiguous, avoiding a guessed lifecycle transition.
    for entry in snapshot:
        decision = decision_by_id[entry["id"]]
        matches = _matching_candidate_record_ids(proposed["records"], entry["pattern"])
        if len(matches) != 1:
            continue
        record = next(item for item in proposed["records"] if item["id"] == matches[0])
        if decision["action"] in {"merge", "promote"}:
            if _below_promotion_floor(record):
                raise ValueError("candidate_promotion_not_eligible")
            record["lifecycle"] = "active"
            record["timestamps"] = {**record["timestamps"], "updated_at": now, "archived_at": None}
        elif decision["action"] == "archive":
            record["lifecycle"] = "archived"
            record["timestamps"] = {**record["timestamps"], "updated_at": now, "archived_at": now}

    cfg = store.load_config(root)
    review = cfg.get("review") if isinstance(cfg, dict) else None
    budgets = validate_active_context_budgets(review)
    bindings = validate_namespace_bindings((review or {}).get("namespace_bindings"))
    binding = bindings.get(namespace or DEFAULT_NAMESPACE, {})
    default_scope = "repository" if binding.get("scope") == "repository" else "global"
    candidates = [
        {
            "record": record,
            "namespace": namespace or DEFAULT_NAMESPACE,
            "scope": _record_context_scope(record, default_scope),
        }
        for record in proposed["records"]
        if record["lifecycle"] in {"active", "pinned"}
    ]
    selection = select_active_context(candidates, budgets)
    overflow = {
        item["record_id"]
        for item in selection["decisions"]
        if item["reason"] != "selected" and item["lifecycle"] == "active"
    }
    if overflow:
        for record in proposed["records"]:
            if record["id"] in overflow:
                record["lifecycle"] = "archived"
                record["timestamps"] = {
                    **record["timestamps"],
                    "updated_at": now,
                    "archived_at": now,
                }
    validate_learning_records_document(proposed)
    return proposed, {
        "governed": True,
        "archived_record_ids": sorted(overflow),
        "selection": {
            "usage": selection["usage"],
            "budgets": selection["budgets"],
            "decisions": selection["decisions"],
        },
    }


def create_consolidation_preview(
    proposal: object,
    *,
    candidate_ids: Sequence[str] | None = None,
    candidate_snapshot: Sequence[dict] | None = None,
    root: Path | None = None,
    namespace: str | None = None,
    now: float | None = None,
) -> dict:
    """Persist a no-write preview from an LLM proposal and immutable local snapshots."""
    ns = namespace or DEFAULT_NAMESPACE
    if ns != DEFAULT_NAMESPACE and not _is_valid_ns_name(ns):
        raise ValueError("invalid_namespace")
    with _candidate_lock(root, ns):
        ns_dir = _namespace_dir(ns, root)
        current_candidates = _list_candidate_unlocked(root, ns)
        if not current_candidates:
            raise ValueError("nothing_to_consolidate")
        available_ids = {str(item["id"]) for item in current_candidates}
        selected_ids = available_ids if candidate_ids is None else set(candidate_ids)
        if (
            not selected_ids
            or any(not isinstance(item, str) or item not in available_ids for item in selected_ids)
            or (candidate_ids is not None and len(set(candidate_ids)) != len(candidate_ids))
        ):
            raise ValueError("invalid_candidate_ids")
        snapshot = []
        for item in candidate_snapshot or current_candidates:
            pattern = item.get("pattern") if candidate_snapshot is not None else item
            if not isinstance(pattern, dict):
                raise ValueError("invalid_candidate_snapshot")
            entry = _candidate_snapshot_entry(pattern)
            if candidate_snapshot is not None and (
                item.get("id") != entry["id"] or item.get("digest") != entry["digest"]
            ):
                raise ValueError("invalid_candidate_snapshot")
            snapshot.append(entry)
        if not _candidate_snapshot_matches(snapshot, current_candidates):
            raise ValueError("candidate_snapshot_changed")
        selected_snapshot = [item for item in snapshot if item["id"] in selected_ids]
        ruleset, decisions = _validate_consolidation_proposal(proposal, selected_ids)
        active_raw = _read_current_text(common_file(root, ns), ns_dir)
        sidecar_path = learning_records_file(root, ns)
        sidecar_raw = _read_current_text(sidecar_path, ns_dir)
        sidecar_document = None
        if sidecar_raw is not None:
            sidecar_document, _ = _read_learning_records_document(sidecar_path, ns_dir)
        proposed_sidecar, budget_impact = _preview_sidecar_document(
            sidecar_document, decisions, selected_snapshot, root=root, namespace=ns
        )
        generated_at = time.time() if now is None else now
        preview_id = str(uuid.uuid4())
        preview = {
            "schema": CONSOLIDATION_PREVIEW_SCHEMA,
            "version": CONSOLIDATION_PREVIEW_VERSION,
            "artifact_generation": 1,
            "preview_id": preview_id,
            "namespace": ns,
            "created_at": _utc_now(),
            "created_at_epoch": generated_at,
            "expires_at_epoch": generated_at + CONSOLIDATION_PREVIEW_TTL_SECONDS,
            "status": "pending_confirmation",
            "candidate_snapshot": snapshot,
            "candidate_snapshot_digest": _sha256_json(snapshot),
            "selected_candidate_ids": sorted(selected_ids),
            "selected_candidate_digest": _sha256_json(selected_snapshot),
            "active_ruleset_generation": _content_generation(active_raw),
            "sidecar_generation": _content_generation(sidecar_raw),
            "proposed_ruleset_markdown": ruleset,
            "proposed_ruleset_digest": _content_generation(ruleset),
            "proposed_sidecar_document": proposed_sidecar,
            "per_candidate_decisions": decisions,
            "budget_impact": budget_impact,
        }
        consolidation_preview_dir(root, ns).mkdir(parents=True, exist_ok=True)
        _atomic_write(
            _preview_path(preview_id, root, ns),
            json.dumps(preview, indent=2, sort_keys=True) + "\n",
        )
    return preview


def _read_consolidation_preview(preview_id: str, root: Path | None, namespace: str | None) -> dict:
    path = _preview_path(preview_id, root, namespace)
    raw = store.read_text_nolink(path, consolidation_preview_dir(root, namespace))
    try:
        preview = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid_preview_artifact") from exc
    required = {
        "schema",
        "version",
        "artifact_generation",
        "preview_id",
        "namespace",
        "created_at",
        "created_at_epoch",
        "expires_at_epoch",
        "status",
        "candidate_snapshot",
        "candidate_snapshot_digest",
        "selected_candidate_ids",
        "selected_candidate_digest",
        "active_ruleset_generation",
        "sidecar_generation",
        "proposed_ruleset_markdown",
        "proposed_ruleset_digest",
        "proposed_sidecar_document",
        "per_candidate_decisions",
        "budget_impact",
    }
    if (
        not isinstance(preview, dict)
        or set(preview) - {"applied_at", "applied_at_epoch", *required}
        or not required.issubset(preview)
        or preview.get("schema") != CONSOLIDATION_PREVIEW_SCHEMA
        or preview.get("version") != CONSOLIDATION_PREVIEW_VERSION
        or preview.get("namespace") != (namespace or DEFAULT_NAMESPACE)
    ):
        raise ValueError("invalid_preview_artifact")
    return preview


def _consolidation_preview_state_unlocked(
    preview: dict,
    *,
    root: Path | None = None,
    namespace: str | None = None,
    now: float | None = None,
) -> dict:
    """Compute expiry and freshness without mutating a proposal artifact."""
    current = time.time() if now is None else now
    ns = namespace or DEFAULT_NAMESPACE
    ns_dir = _namespace_dir(ns, root)
    active_raw = _read_current_text(common_file(root, ns), ns_dir)
    sidecar_raw = _read_current_text(learning_records_file(root, ns), ns_dir)
    candidates = _list_candidate_unlocked(root, ns)
    stale_reasons: list[str] = []
    if _content_generation(active_raw) != preview["active_ruleset_generation"]:
        stale_reasons.append("active_ruleset_changed")
    if _content_generation(sidecar_raw) != preview["sidecar_generation"]:
        stale_reasons.append("sidecar_changed")
    if not _candidate_snapshot_matches(preview["candidate_snapshot"], candidates):
        stale_reasons.append("candidate_snapshot_changed")
    return {
        "preview_id": preview["preview_id"],
        "namespace": ns,
        "status": preview["status"],
        "expired": current >= preview["expires_at_epoch"],
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "expires_at_epoch": preview["expires_at_epoch"],
    }


def consolidation_preview_state(
    preview: dict,
    *,
    root: Path | None = None,
    namespace: str | None = None,
    now: float | None = None,
) -> dict:
    with _candidate_lock(root, namespace):
        return _consolidation_preview_state_unlocked(
            preview, root=root, namespace=namespace, now=now
        )


def get_consolidation_preview(
    preview_id: str,
    *,
    root: Path | None = None,
    namespace: str | None = None,
    now: float | None = None,
) -> dict:
    preview = _read_consolidation_preview(preview_id, root, namespace)
    return {
        **preview,
        "state": consolidation_preview_state(preview, root=root, namespace=namespace, now=now),
    }


def list_consolidation_previews(
    *, root: Path | None = None, namespace: str | None = None, now: float | None = None
) -> list[dict]:
    directory = consolidation_preview_dir(root, namespace)
    if not directory.is_dir():
        return []
    previews: list[dict] = []
    for path in directory.glob("*.v1.json"):
        try:
            preview = _read_consolidation_preview(
                path.name.removesuffix(".v1.json"), root, namespace
            )
            previews.append(
                {
                    "preview_id": preview["preview_id"],
                    "namespace": preview["namespace"],
                    "created_at": preview["created_at"],
                    "selected_candidate_ids": preview["selected_candidate_ids"],
                    "state": consolidation_preview_state(
                        preview, root=root, namespace=namespace, now=now
                    ),
                }
            )
        except (OSError, ValueError):
            continue
    return sorted(previews, key=lambda item: str(item["created_at"]), reverse=True)


def apply_consolidation_preview(
    preview_id: str,
    *,
    confirmed: bool,
    root: Path | None = None,
    namespace: str | None = None,
    now: float | None = None,
) -> dict:
    """Confirm one fresh preview through a recoverable durable transaction."""
    if not confirmed:
        return {"ok": False, "code": "confirmation_required"}
    ns = namespace or DEFAULT_NAMESPACE
    with _candidate_lock(root, ns):
        preview = _read_consolidation_preview(preview_id, root, ns)
        if preview["status"] == "applied":
            return {"ok": False, "code": "preview_already_applied"}
        state = _consolidation_preview_state_unlocked(preview, root=root, namespace=ns, now=now)
        if state["expired"]:
            return {"ok": False, "code": "preview_expired", "state": state}
        if state["stale"]:
            return {"ok": False, "code": "preview_stale", "state": state}
        ns_dir = _namespace_dir(ns, root)
        current_candidates = _list_candidate_unlocked(root, ns)
        consumed_ids = {
            item["candidate_id"]
            for item in preview["per_candidate_decisions"]
            if item["action"] != "retain"
        }
        consumed = [item for item in preview["candidate_snapshot"] if item["id"] in consumed_ids]
        candidate_body = _candidate_body_after_consuming(current_candidates, consumed)
        live_path = common_file(root, ns)
        sidecar_path = learning_records_file(root, ns)
        candidate_path = candidate_file(root, ns)
        paths = _preview_transaction_paths(preview_id, root, ns)
        before = {
            "live": _read_current_text(live_path, ns_dir),
            "sidecar": _read_current_text(sidecar_path, ns_dir),
            "candidate": _read_current_text(candidate_path, ns_dir),
            "preview": _read_current_text(_preview_path(preview_id, root, ns), ns_dir),
        }
        try:
            applied_preview = {
                **preview,
                "status": "applied",
                "artifact_generation": preview["artifact_generation"] + 1,
                "applied_at": _utc_now(),
                "applied_at_epoch": time.time() if now is None else now,
            }
            after = {
                "live": preview["proposed_ruleset_markdown"],
                "sidecar": (
                    json.dumps(preview["proposed_sidecar_document"], indent=2, sort_keys=True)
                    + "\n"
                    if preview["proposed_sidecar_document"] is not None
                    else before["sidecar"]
                ),
                "candidate": candidate_body,
                "preview": json.dumps(applied_preview, indent=2, sort_keys=True) + "\n",
            }
            transaction = {
                "schema": _PREVIEW_TRANSACTION_SCHEMA,
                "version": _PREVIEW_TRANSACTION_VERSION,
                "status": "prepared",
                "preview_id": preview_id,
                "before": before,
                "after": after,
            }
            _write_preview_transaction(root, ns, transaction)
            _write_preview_snapshot(paths, after)
            transaction["status"] = "committed"
            _write_preview_transaction(root, ns, transaction)
            _preview_transaction_path(root, ns).unlink(missing_ok=True)
        except Exception as exc:
            try:
                _recover_consolidation_preview_transaction(root, ns)
            except Exception as rollback_exc:
                return {
                    "ok": False,
                    "code": "apply_rollback_failed",
                    "error_code": type(rollback_exc).__name__,
                }
            return {"ok": False, "code": "apply_rolled_back", "error_code": type(exc).__name__}
        return {
            "ok": True,
            "code": "preview_applied",
            "preview_id": preview_id,
            "namespace": ns,
            "consumed_candidate_ids": sorted(consumed_ids),
            "retained_candidate_ids": sorted(set(preview["selected_candidate_ids"]) - consumed_ids),
            "budget_impact": preview["budget_impact"],
        }


# ---------------------------------------------------------------------------
# Seed set — deliberately MINIMAL (bootstraps the common warm-start layer)
# ---------------------------------------------------------------------------

DEFAULT_SEED_PATTERNS: list[dict] = [
    {
        "title": "Reset guard flags on every exit path",
        "scope": "common",
        "impact": "high",
        "dimension": "correctness",
        "seed": True,
        "guidance": "When a boolean guard gates a loop or state machine, ensure it is reset on ALL exit paths, including early returns and exceptions, so the next cycle never reads a stale invariant.",
    },
    {
        "title": "Authorize by confirming the owner, not by rejecting known-bad",
        "scope": "common",
        "impact": "high",
        "dimension": "security",
        "seed": True,
        "guidance": "Authorization must positively confirm the authenticated principal. Negative-only checks (reject known-bad, reject if disabled) are fail-open — any unanticipated caller passes.",
    },
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
    ap = argparse.ArgumentParser(
        description="Code Review Sage learning store (V2, file-centric + namespaces)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed").add_argument("--force", action="store_true")
    lp = sub.add_parser("list-patterns")
    lp.add_argument("--namespace", default=None, help="namespace to list (default: 'default')")
    lc = sub.add_parser("list-candidate")
    lc.add_argument("--namespace", default=None)
    sp = sub.add_parser(
        "stage",
        help="stage a new learning in the candidate file "
        "(an exact equivalent bumps its seen count)",
    )
    sp.add_argument("--file", required=True, help="JSON pattern file")
    sp.add_argument("--source", required=True, choices=sorted(ADMISSIBLE_SOURCES))
    sp.add_argument("--namespace", default=None)
    cp = sub.add_parser(
        "consolidate", help="apply the AI-merged learned-patterns.md and clear the candidate"
    )
    cp.add_argument(
        "--merged-file", required=True, help="file holding the AI-merged learned-patterns.md"
    )
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


# Keep the original module as the import and monkeypatch surface.
from kiro_crew import _facade_mirror  # noqa: E402 — preserve the facade surface

from . import learning_format as _learning_format  # noqa: E402 — preserve the facade surface

_OWNERS = (_learning_format.__name__,)
_facade_mirror.install(sys.modules[__name__], _OWNERS)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule


if __name__ == "__main__":
    raise SystemExit(_main())
