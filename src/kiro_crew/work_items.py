"""Durable, coordinator-scoped work items and closed-cycle archives.

The per-session work ledger records one session's resumable intent. This module
records the bounded work items that a coordinator owns for one active cycle.
The two stores share only their exact session-key identity and lifecycle: work
items are structured product data, not strings embedded in ledger artifacts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from kiro_crew import session_ledger
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import release_lock, try_acquire_lock
from kiro_crew.session_ledger import ledger_key

SCHEMA_VERSION = 1

STATE_PROPOSED = "proposed"
STATE_WAITING = "waiting"
STATE_DISPATCHED = "dispatched"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_ACCEPTED, STATE_REJECTED, STATE_CANCELLED})
COORDINATOR_TRANSITIONS = frozenset(
    {STATE_PROPOSED, STATE_WAITING, STATE_REJECTED, STATE_CANCELLED}
)

ASSIGNMENT_PENDING_DELIVERY = "pending_delivery"
ASSIGNMENT_LAUNCH_QUEUED = "launch_queued"
ASSIGNMENT_DELIVERED = "delivered"
ASSIGNMENT_FAILED = "failed"
ASSIGNMENT_REVOKED = "revoked"
ASSIGNMENT_STATUSES = frozenset(
    {
        ASSIGNMENT_PENDING_DELIVERY,
        ASSIGNMENT_LAUNCH_QUEUED,
        ASSIGNMENT_DELIVERED,
        ASSIGNMENT_FAILED,
        ASSIGNMENT_REVOKED,
    }
)
# The statuses a worker may act on: intent exists and nothing was revoked or
# terminally failed. ``delivered`` is the launched child's live window.
ASSIGNMENT_ACTIVE = frozenset(
    {ASSIGNMENT_PENDING_DELIVERY, ASSIGNMENT_LAUNCH_QUEUED, ASSIGNMENT_DELIVERED}
)
ASSIGNMENT_SOURCE_LAUNCHED = "launched_subagent"
ASSIGNMENT_SOURCE_EXISTING = "existing_session"

_STATE_FILE = "state.json"
_KEY_FILE = "coordinator_key"
_LOCK_FILE = ".lock"
_ARCHIVE_DIR = "archive"
_STORE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_STORE_NAME_READABLE_MAX = 80

_MAX_TITLE = 240
_MAX_TEXT = 2_000
_MAX_REF = 2_000
_MAX_RESOURCE = 512
_MAX_RESOURCES = 32
_MAX_ITEMS = 64
_MAX_ITEM_EVENTS = 32
_MAX_EVIDENCE = 2_000
_MAX_STATE_BYTES = 1_000_000
_MAX_ARCHIVES = 64
_MAX_ARCHIVE_BYTES = 4_000_000
_MAX_CONTRACT_BYTES = 32_768
_MAX_ATTEMPTS = 32
_MAX_FAILURE_CODE = 64
_MAX_VERIFICATION = 8
_MAX_VERIFICATION_ENTRY = 512
_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

_EVENT_KINDS = frozenset(
    {
        "created",
        "progress",
        "blocker",
        "state",
        "evaluation",
        "assignment",
        "worker_handoff",
        "runtime",
    }
)
_LEGACY_ITEM_KEY_RE = re.compile(r"item-[0-9]+\Z")
_LEGACY_STATUSES = frozenset({"running", "waiting", "pass", "fail"})
_ITEM_ID_RE = re.compile(r"wi_[0-9a-f]{32}\Z")
_CYCLE_ID_RE = re.compile(r"wc_[0-9a-f]{32}\Z")
_ASSIGNMENT_ID_RE = re.compile(r"wa_[0-9a-f]{32}\Z")
_RUN_ID_RE = re.compile(r"[0-9a-f]{8}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}\Z")

logger = logging.getLogger(__name__)


class WorkItemError(ValueError):
    """Base error for a rejected work-item operation."""


class WorkItemNotFound(WorkItemError):
    """The requested item or archive does not exist."""


class WorkItemStoreCorrupt(WorkItemError):
    """Persisted work-item data cannot safely be interpreted."""


class WorkItemArchiveFull(WorkItemError):
    """Closing would exceed the store's deliberate archive retention bound."""


class WorkItemAssignmentDenied(WorkItemError):
    """The calling worker does not own the requested assignment."""


class WorkItemAssignmentStale(WorkItemError):
    """The assignment is no longer live for the calling worker."""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text(value: Any, limit: int, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise WorkItemError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise WorkItemError(f"{field} must not be empty")
    if len(result) > limit:
        raise WorkItemError(f"{field} exceeds its maximum length")
    return result


def _store_name(coordinator_key: str) -> str:
    readable = _STORE_NAME_UNSAFE.sub("_", coordinator_key)[:_STORE_NAME_READABLE_MAX]
    digest = hashlib.sha256(coordinator_key.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def _root() -> Path:
    """Resolve the live work-item root rather than freezing a test override."""
    return data_home() / "work-items"


def coordinator_dir(session_key: str) -> Path:
    """Return the validated directory for one coordinator session key."""
    key = ledger_key(session_key)
    if not key or "\0" in key or "/" in key or "\\" in key:
        raise WorkItemError(f"invalid coordinator session key: {session_key!r}")
    root = _root()
    candidate = (root / _store_name(key)).resolve()
    if candidate == root.resolve() or not candidate.is_relative_to(root.resolve()):
        raise WorkItemError(f"work-item path traversal blocked for key: {session_key!r}")
    return candidate


def _archive_dir(dir_path: Path) -> Path:
    archive = (dir_path / _ARCHIVE_DIR).resolve()
    if not archive.is_relative_to(dir_path.resolve()):
        raise WorkItemError("work-item archive path escaped its coordinator store")
    return archive


@contextmanager
def _locked(dir_path: Path) -> Iterator[None]:
    """Acquire one coordinator's lock with a bounded wait."""
    dir_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(dir_path / _LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
        while not try_acquire_lock(fd, exclusive=True):
            if time.monotonic() >= deadline:
                raise OSError("work-item store lock is held; try again")
            time.sleep(_LOCK_POLL_SECS)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "active_cycle": None,
        "migration": {"completed": False, "warnings": []},
        "created_at": "",
        "updated_at": "",
    }


def _state_path(dir_path: Path) -> Path:
    return dir_path / _STATE_FILE


def _read_json(path: Path, description: str, *, max_bytes: int = _MAX_STATE_BYTES) -> Any:
    try:
        if path.stat().st_size > max_bytes:
            raise WorkItemStoreCorrupt(f"{description} exceeds the storage limit")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkItemStoreCorrupt(f"{description} is unreadable") from exc


def _coerce_event(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise WorkItemStoreCorrupt("work-item event is not an object")
    ts = raw.get("ts")
    kind = raw.get("kind")
    text = raw.get("text")
    if not all(isinstance(value, str) for value in (ts, kind, text)):
        raise WorkItemStoreCorrupt("work-item event has invalid fields")
    # ``actor`` is the server-stamped worker fingerprint for worker-authored
    # events; coordinator events carry "". Slice 2 events predate it, so a
    # missing value decodes as the empty actor rather than corruption.
    actor = raw.get("actor", "")
    if not isinstance(actor, str) or len(actor) > 64:
        raise WorkItemStoreCorrupt("work-item event actor is invalid")
    return {"ts": ts, "kind": kind, "text": text, "actor": actor}


def _normalize_assignment(raw: Any) -> dict[str, Any] | None:
    """Strictly decode one assignment record; Slice 2 items decode as ``None``."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkItemStoreCorrupt("work-item assignment is not an object")
    for field in (
        "id",
        "source",
        "worker_session_key",
        "worker_fingerprint",
        "worker_slot",
        "worker_run_id",
        "candidate_fingerprint",
        "status",
        "contract_digest",
        "created_at",
        "delivered_at",
        "failed_at",
        "failure_code",
    ):
        if not isinstance(raw.get(field), str):
            raise WorkItemStoreCorrupt("work-item assignment has invalid fields")
    if not _ASSIGNMENT_ID_RE.fullmatch(raw["id"]):
        raise WorkItemStoreCorrupt("work-item assignment has an invalid ID")
    if raw["source"] not in {ASSIGNMENT_SOURCE_LAUNCHED, ASSIGNMENT_SOURCE_EXISTING}:
        raise WorkItemStoreCorrupt("work-item assignment has an unknown source")
    if raw["status"] not in ASSIGNMENT_STATUSES:
        raise WorkItemStoreCorrupt("work-item assignment has an unknown status")
    if not raw["worker_session_key"]:
        raise WorkItemStoreCorrupt("work-item assignment names no worker identity")
    if bool(raw["worker_slot"]) == bool(raw["worker_run_id"]):
        raise WorkItemStoreCorrupt("work-item assignment names no worker or two")
    if not _FINGERPRINT_RE.fullmatch(raw["worker_fingerprint"]):
        raise WorkItemStoreCorrupt("work-item assignment fingerprint is invalid")
    if raw["worker_run_id"] and not _RUN_ID_RE.fullmatch(raw["worker_run_id"]):
        raise WorkItemStoreCorrupt("work-item assignment run ID is invalid")
    if raw["candidate_fingerprint"] and not _FINGERPRINT_RE.fullmatch(
        raw["candidate_fingerprint"]
    ):
        raise WorkItemStoreCorrupt("work-item assignment candidate is invalid")
    attempt = raw.get("attempt")
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= _MAX_ATTEMPTS
    ):
        raise WorkItemStoreCorrupt("work-item assignment attempt is invalid")
    if raw["contract_digest"] and not _DIGEST_RE.fullmatch(raw["contract_digest"]):
        raise WorkItemStoreCorrupt("work-item assignment digest is invalid")
    if raw["failure_code"] and len(raw["failure_code"]) > _MAX_FAILURE_CODE:
        raise WorkItemStoreCorrupt("work-item assignment failure code is too long")
    return {
        "id": raw["id"],
        "source": raw["source"],
        "worker_session_key": raw["worker_session_key"],
        "worker_fingerprint": raw["worker_fingerprint"],
        "worker_slot": raw["worker_slot"],
        "worker_run_id": raw["worker_run_id"],
        "candidate_fingerprint": raw["candidate_fingerprint"],
        "status": raw["status"],
        "attempt": attempt,
        "contract_digest": raw["contract_digest"],
        "created_at": raw["created_at"],
        "delivered_at": raw["delivered_at"],
        "failed_at": raw["failed_at"],
        "failure_code": raw["failure_code"],
    }


def _normalize_handoff(raw: Any) -> dict[str, Any] | None:
    """Strictly decode the latest worker handoff; Slice 2 items decode as ``None``."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkItemStoreCorrupt("worker handoff is not an object")
    for field in (
        "outcome",
        "canonical_ref",
        "next_action",
        "blocker",
        "release_condition",
        "at",
        "actor",
    ):
        if not isinstance(raw.get(field), str):
            raise WorkItemStoreCorrupt("worker handoff has invalid fields")
    verification = raw.get("verification")
    if (
        not isinstance(verification, list)
        or not verification
        or len(verification) > _MAX_VERIFICATION
        or not all(isinstance(entry, str) and entry for entry in verification)
    ):
        raise WorkItemStoreCorrupt("worker handoff verification is invalid")
    if not raw["outcome"].strip() or not raw["next_action"].strip():
        raise WorkItemStoreCorrupt("worker handoff is missing required text")
    if bool(raw["blocker"].strip()) != bool(raw["release_condition"].strip()):
        raise WorkItemStoreCorrupt(
            "worker handoff blocker and release condition are unpaired"
        )
    return {
        "outcome": raw["outcome"],
        "canonical_ref": raw["canonical_ref"],
        "next_action": raw["next_action"],
        "verification": list(verification),
        "blocker": raw["blocker"],
        "release_condition": raw["release_condition"],
        "at": raw["at"],
        "actor": raw["actor"],
    }


def _coerce_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkItemStoreCorrupt("work item is not an object")
    required_strings = (
        "id",
        "title",
        "state",
        "canonical_ref",
        "next_action",
        "created_at",
        "updated_at",
        "finished_at",
    )
    if not all(isinstance(raw.get(field), str) for field in required_strings):
        raise WorkItemStoreCorrupt("work item has invalid fields")
    if not _ITEM_ID_RE.fullmatch(raw["id"]):
        raise WorkItemStoreCorrupt("work item has an invalid ID")
    if raw["state"] not in TERMINAL_STATES | {
        STATE_PROPOSED,
        STATE_WAITING,
        STATE_DISPATCHED,
    }:
        raise WorkItemStoreCorrupt("work item has an unknown state")
    try:
        acceptance = _normalize_acceptance(raw.get("acceptance"))
        resources = _normalize_resources(raw.get("declared_resources", []))
    except WorkItemError as exc:
        raise WorkItemStoreCorrupt("work item has invalid immutable fields") from exc
    events = raw.get("events")
    if not isinstance(events, list):
        raise WorkItemStoreCorrupt("work item events are invalid")
    last_evaluation = raw.get("last_evaluation")
    if last_evaluation is not None and not isinstance(last_evaluation, dict):
        raise WorkItemStoreCorrupt("work item evaluation is invalid")
    provenance = raw.get("migration_provenance")
    if provenance is not None and not isinstance(provenance, dict):
        raise WorkItemStoreCorrupt("work item migration provenance is invalid")
    assignment = _normalize_assignment(raw.get("assignment"))
    handoff = _normalize_handoff(raw.get("worker_handoff"))
    item = {
        "id": raw["id"],
        "title": raw["title"],
        "state": raw["state"],
        "acceptance": acceptance,
        "canonical_ref": raw["canonical_ref"],
        "declared_resources": resources,
        "next_action": raw["next_action"],
        "events": [_coerce_event(event) for event in events[-_MAX_ITEM_EVENTS:]],
        "last_evaluation": copy.deepcopy(last_evaluation),
        "migration_provenance": copy.deepcopy(provenance),
        "assignment": assignment,
        "worker_handoff": handoff,
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
        "finished_at": raw["finished_at"],
    }
    return item


def _coerce_cycle(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkItemStoreCorrupt("active work cycle is not an object")
    for field in ("id", "goal", "next_action", "opened_at", "updated_at"):
        if not isinstance(raw.get(field), str):
            raise WorkItemStoreCorrupt("active work cycle has invalid fields")
    if not _CYCLE_ID_RE.fullmatch(raw["id"]):
        raise WorkItemStoreCorrupt("active work cycle has an invalid ID")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) > _MAX_ITEMS:
        raise WorkItemStoreCorrupt("active work cycle has invalid items")
    parsed = [_coerce_item(item) for item in items]
    if len({item["id"] for item in parsed}) != len(parsed):
        raise WorkItemStoreCorrupt("active work cycle has duplicate item IDs")
    return {
        "id": raw["id"],
        "goal": raw["goal"],
        "next_action": raw["next_action"],
        "opened_at": raw["opened_at"],
        "updated_at": raw["updated_at"],
        "items": parsed,
    }


def _coerce_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkItemStoreCorrupt("work-item state is not an object")
    schema = raw.get("schema")
    if schema != SCHEMA_VERSION or isinstance(schema, bool):
        raise WorkItemStoreCorrupt("work-item state has an invalid schema")
    migration = raw.get("migration", {})
    if not isinstance(migration, dict) or not isinstance(migration.get("completed", False), bool):
        raise WorkItemStoreCorrupt("work-item migration state is invalid")
    warnings = migration.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(value, str) for value in warnings):
        raise WorkItemStoreCorrupt("work-item migration warnings are invalid")
    for field in ("created_at", "updated_at"):
        if not isinstance(raw.get(field, ""), str):
            raise WorkItemStoreCorrupt("work-item state timestamps are invalid")
    return {
        "schema": schema,
        "active_cycle": _coerce_cycle(raw.get("active_cycle")),
        "migration": {
            "completed": migration.get("completed", False),
            "warnings": warnings[:_MAX_ITEMS],
        },
        "created_at": raw.get("created_at", ""),
        "updated_at": raw.get("updated_at", ""),
    }


def _read_state_unlocked(dir_path: Path) -> dict[str, Any]:
    path = _state_path(dir_path)
    try:
        raw = _read_json(path, "work-item state")
    except FileNotFoundError:
        return _empty_state()
    return _coerce_state(raw)


def _legacy_warning(key: str, reason: str) -> str:
    """Return a bounded, field-free record of one skipped legacy artifact."""
    return f"{key}: {reason}"[:_MAX_TEXT]


def _legacy_item(key: str, raw_value: Any, now: str) -> tuple[dict[str, Any] | None, str | None]:
    """Map one documented Goal Conductor artifact without guessing its contract."""
    if not isinstance(raw_value, str):
        return None, _legacy_warning(key, "legacy value is not a string")
    try:
        legacy = json.loads(raw_value)
    except (json.JSONDecodeError, RecursionError):
        return None, _legacy_warning(key, "legacy value is not valid JSON")
    if not isinstance(legacy, dict):
        return None, _legacy_warning(key, "legacy value is not an object")
    status = legacy.get("status")
    child_session = legacy.get("session")
    acceptance = legacy.get("accept")
    if status not in _LEGACY_STATUSES:
        return None, _legacy_warning(key, "legacy status is not recognized")
    if not isinstance(child_session, str) or not child_session.strip():
        return None, _legacy_warning(key, "legacy session is missing")
    try:
        normalized_acceptance = _normalize_acceptance(acceptance)
    except WorkItemError:
        return None, _legacy_warning(key, "legacy acceptance is unsupported")

    target_state = {
        "running": STATE_WAITING,
        "waiting": STATE_WAITING,
        "pass": STATE_ACCEPTED,
        "fail": STATE_REJECTED,
    }[status]
    since = legacy.get("since")
    round_value = legacy.get("round")
    fails = legacy.get("fails")
    provenance: dict[str, Any] = {"legacy_key": key, "session": child_session, "status": status}
    if isinstance(since, str) and len(since) <= _MAX_REF:
        provenance["since"] = since
    if isinstance(round_value, int) and not isinstance(round_value, bool) and round_value >= 0:
        provenance["round"] = round_value
    if isinstance(fails, int) and not isinstance(fails, bool) and fails >= 0:
        provenance["fails"] = fails
    if target_state in TERMINAL_STATES:
        next_action = "Imported terminal outcome; review the closed-cycle archive if needed."
        finished_at = now
    else:
        next_action = f"Inspect legacy child session {child_session} before continuing this item."
        finished_at = ""
    item = {
        "id": _new_id("wi"),
        "title": f"Migrated Goal Conductor item {key}",
        "state": target_state,
        "acceptance": normalized_acceptance,
        "canonical_ref": "",
        "declared_resources": [],
        "next_action": next_action,
        "events": [
            {
                "ts": now,
                "kind": "created",
                "text": f"imported from legacy Goal Conductor artifact {key}",
            }
        ],
        "last_evaluation": None,
        "assignment": None,
        "worker_handoff": None,
        "migration_provenance": provenance,
        "created_at": now,
        "updated_at": now,
        "finished_at": finished_at,
    }
    return item, None


def _write_archive_unlocked(
    dir_path: Path,
    cycle: dict[str, Any],
    *,
    summary: str,
) -> dict[str, Any]:
    """Write or verify a cycle archive while the caller holds the store lock."""
    archive_path = _archive_path(dir_path, cycle["id"])
    cycle_digest = _cycle_digest(cycle)
    if archive_path.exists():
        archive = _read_archive(archive_path)
        if archive.get("source_cycle_digest") != cycle_digest:
            raise WorkItemStoreCorrupt("existing archive does not match the active cycle")
        return archive
    count, used = _archive_usage(dir_path)
    archive = {
        "schema": SCHEMA_VERSION,
        "cycle": copy.deepcopy(cycle),
        "summary": summary,
        "closed_at": _now_iso(),
        "source_cycle_digest": cycle_digest,
    }
    encoded = json.dumps(archive, ensure_ascii=False, sort_keys=True)
    if count >= _MAX_ARCHIVES or used + len(encoded.encode("utf-8")) > _MAX_ARCHIVE_BYTES:
        raise WorkItemArchiveFull("closed-cycle archive is full; retain or delete archives first")
    _archive_dir(dir_path).mkdir(parents=True, exist_ok=True)
    atomic_write(archive_path, encoded + "\n", mode=0o600)
    return archive


def _migrate_legacy_unlocked(
    dir_path: Path, coordinator_key: str, state: dict[str, Any]
) -> dict[str, Any]:
    """Import the old string-artifact protocol exactly once under the store lock."""
    if state["migration"]["completed"]:
        return state
    if state["active_cycle"] is not None:
        raise WorkItemStoreCorrupt("cannot import legacy work items over an active cycle")

    # The ledger itself is only a migration source, not an authority here. Its
    # tolerant read is intentional: malformed legacy state yields no guessed
    # work, while the migration marker prevents every later read re-parsing it.
    artifacts = session_ledger.read_state(coordinator_key).get("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}
    now = _now_iso()
    imported: list[dict[str, Any]] = []
    warnings: list[str] = []
    for artifact_key, raw_value in artifacts.items():
        if not isinstance(artifact_key, str) or not _LEGACY_ITEM_KEY_RE.fullmatch(artifact_key):
            continue
        item, warning = _legacy_item(artifact_key, raw_value, now)
        if item is not None:
            imported.append(item)
        elif warning is not None:
            warnings.append(warning)
    state["migration"] = {"completed": True, "warnings": warnings[:_MAX_ITEMS]}
    if imported:
        cycle = {
            "id": _new_id("wc"),
            "goal": "Migrated Goal Conductor work items",
            "next_action": "Review imported items and decide their next coordination step.",
            "opened_at": now,
            "updated_at": now,
            "items": imported,
        }
        if all(item["state"] in TERMINAL_STATES for item in imported):
            _write_archive_unlocked(
                dir_path,
                cycle,
                summary="Imported terminal Goal Conductor item outcomes.",
            )
        else:
            state["active_cycle"] = cycle
    _write_state(dir_path, coordinator_key, state)
    return state


def ensure_migrated(session_key: str) -> dict[str, Any]:
    """Run the idempotent legacy import before the first product operation."""
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        return copy.deepcopy(_migrate_legacy_unlocked(dir_path, key, state))


def _serialize_state(state: dict[str, Any]) -> str:
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > _MAX_STATE_BYTES - 4096:
        raise WorkItemError("work-item state exceeds the storage limit")
    return encoded


def _write_state(dir_path: Path, coordinator_key: str, state: dict[str, Any]) -> None:
    now = _now_iso()
    if not state["created_at"]:
        state["created_at"] = now
        atomic_write(dir_path / _KEY_FILE, coordinator_key + "\n", mode=0o600)
    state["updated_at"] = now
    state["schema"] = SCHEMA_VERSION
    atomic_write(_state_path(dir_path), _serialize_state(state) + "\n", mode=0o600)


def _normalize_resources(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkItemError("declared_resources must be an array of strings")
    if len(value) > _MAX_RESOURCES:
        raise WorkItemError("declared_resources exceeds its item limit")
    resources = [
        _text(entry, _MAX_RESOURCE, "declared_resources entry", required=True) for entry in value
    ]
    if len(set(resources)) != len(resources):
        raise WorkItemError("declared_resources must not contain duplicates")
    return resources


def _normalize_acceptance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkItemError("acceptance must be an object")
    kind = value.get("kind")
    if kind == "pr_checks":
        if set(value) != {"kind", "repo", "pr"}:
            raise WorkItemError("pr_checks acceptance has unsupported fields")
        repo = _text(value.get("repo"), 512, "acceptance.repo", required=True)
        pr = value.get("pr")
        if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
            raise WorkItemError("acceptance.pr must be a positive integer")
        return {"kind": kind, "repo": repo, "pr": pr}
    if kind == "file":
        allowed = {"kind", "path", "exists"}
        if not set(value).issubset(allowed) or "path" not in value:
            raise WorkItemError("file acceptance has unsupported fields")
        path = _text(value.get("path"), _MAX_REF, "acceptance.path", required=True)
        exists = value.get("exists", True)
        if not isinstance(exists, bool):
            raise WorkItemError("acceptance.exists must be a boolean")
        return {"kind": kind, "path": path, "exists": exists}
    if kind == "human_approval":
        if set(value) != {"kind"}:
            raise WorkItemError("human_approval acceptance has unsupported fields")
        return {"kind": kind}
    if kind == "cmd":
        raise WorkItemError("acceptance may not name a command")
    raise WorkItemError("acceptance kind is not supported")


def _find_item(cycle: dict[str, Any], item_id: str) -> dict[str, Any]:
    if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
        raise WorkItemError("invalid work item ID")
    for item in cycle["items"]:
        if item["id"] == item_id:
            return item
    raise WorkItemNotFound(f"work item {item_id!r} was not found")


def _append_event(
    item: dict[str, Any], *, kind: str, text: str, now: str, actor: str = ""
) -> None:
    if kind not in _EVENT_KINDS:
        raise WorkItemError("work-item event kind is not supported")
    item["events"] = (
        list(item["events"])
        + [{"ts": now, "kind": kind, "text": text, "actor": actor}][-_MAX_ITEM_EVENTS:]
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _cycle_digest(cycle: dict[str, Any]) -> str:
    encoded = json.dumps(cycle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_state(session_key: str) -> dict[str, Any]:
    """Read the caller's store; a missing store is empty, corruption is visible."""
    # Reading is the public migration trigger: an old coordinator cannot open a
    # fresh cycle over uninspected ``item-N`` artifacts just because its first
    # post-upgrade operation happened to be a list or open call.
    return ensure_migrated(session_key)


def open_cycle(session_key: str, *, goal: str, next_action: str) -> dict[str, Any]:
    """Create the one active cycle for a coordinator."""
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        if state["active_cycle"] is not None:
            raise WorkItemError("a work cycle is already active")
        now = _now_iso()
        cycle = {
            "id": _new_id("wc"),
            "goal": _text(goal, _MAX_TEXT, "goal", required=True),
            "next_action": _text(next_action, _MAX_TEXT, "next_action", required=True),
            "opened_at": now,
            "updated_at": now,
            "items": [],
        }
        state["active_cycle"] = cycle
        _write_state(dir_path, key, state)
        return copy.deepcopy(cycle)


def active_cycle(session_key: str) -> dict[str, Any] | None:
    """Return the active cycle without creating one."""
    return copy.deepcopy(read_state(session_key)["active_cycle"])


def create_item(
    session_key: str,
    *,
    title: str,
    acceptance: dict[str, Any],
    next_action: str,
    canonical_ref: str = "",
    declared_resources: list[str] | None = None,
) -> dict[str, Any]:
    """Add one proposed item to the coordinator's active cycle."""
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemError("open a work cycle before creating an item")
        if len(cycle["items"]) >= _MAX_ITEMS:
            raise WorkItemError("the active work cycle is at its item limit")
        now = _now_iso()
        item = {
            "id": _new_id("wi"),
            "title": _text(title, _MAX_TITLE, "title", required=True),
            "state": STATE_PROPOSED,
            "acceptance": _normalize_acceptance(acceptance),
            "canonical_ref": _text(canonical_ref, _MAX_REF, "canonical_ref"),
            "declared_resources": _normalize_resources(declared_resources),
            "next_action": _text(next_action, _MAX_TEXT, "next_action", required=True),
            "events": [],
            "last_evaluation": None,
            "assignment": None,
            "worker_handoff": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": "",
        }
        _append_event(item, kind="created", text="work item created", now=now)
        cycle["items"].append(item)
        cycle["updated_at"] = now
        _write_state(dir_path, key, state)
        return copy.deepcopy(item)


def list_items(session_key: str) -> list[dict[str, Any]]:
    """Return the current cycle's items, or an empty list without a cycle."""
    cycle = active_cycle(session_key)
    return [] if cycle is None else copy.deepcopy(cycle["items"])


def read_item(session_key: str, item_id: str) -> dict[str, Any]:
    """Read one active item by opaque ID."""
    cycle = active_cycle(session_key)
    if cycle is None:
        raise WorkItemNotFound(f"work item {item_id!r} was not found")
    return copy.deepcopy(_find_item(cycle, item_id))


def update_item(
    session_key: str,
    item_id: str,
    *,
    canonical_ref: str | None = None,
    declared_resources: list[str] | None = None,
    next_action: str | None = None,
    event: str | None = None,
    event_kind: str = "progress",
) -> dict[str, Any]:
    """Update mutable coordinator-owned fields without changing acceptance/state."""
    if all(value is None for value in (canonical_ref, declared_resources, next_action, event)):
        raise WorkItemError("pass at least one mutable work-item field")
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemNotFound(f"work item {item_id!r} was not found")
        item = _find_item(cycle, item_id)
        now = _now_iso()
        if canonical_ref is not None:
            item["canonical_ref"] = _text(canonical_ref, _MAX_REF, "canonical_ref")
        if declared_resources is not None:
            item["declared_resources"] = _normalize_resources(declared_resources)
        if next_action is not None:
            item["next_action"] = _text(next_action, _MAX_TEXT, "next_action", required=True)
        if event is not None:
            _append_event(
                item,
                kind=event_kind,
                text=_text(event, _MAX_TEXT, "event", required=True),
                now=now,
            )
        item["updated_at"] = now
        cycle["updated_at"] = now
        _write_state(dir_path, key, state)
        return copy.deepcopy(item)


def transition_item(
    session_key: str,
    item_id: str,
    *,
    state_name: str,
    event: str,
    next_action: str | None = None,
) -> dict[str, Any]:
    """Move one non-terminal item through the coordinator-only state machine."""
    if state_name not in COORDINATOR_TRANSITIONS:
        raise WorkItemError("the coordinator may not transition an item to that state")
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemNotFound(f"work item {item_id!r} was not found")
        item = _find_item(cycle, item_id)
        if item["state"] in TERMINAL_STATES:
            raise WorkItemError("a terminal work item cannot transition again")
        now = _now_iso()
        item["state"] = state_name
        if next_action is not None:
            item["next_action"] = _text(next_action, _MAX_TEXT, "next_action", required=True)
        item["finished_at"] = now if state_name in TERMINAL_STATES else ""
        _append_event(
            item,
            kind="state",
            text=_text(event, _MAX_TEXT, "event", required=True),
            now=now,
        )
        item["updated_at"] = now
        cycle["updated_at"] = now
        _write_state(dir_path, key, state)
        return copy.deepcopy(item)


def evaluate_items(
    session_key: str,
    item_ids: list[str],
    *,
    evaluator: Callable[[dict[str, Any]], tuple[str, str]],
) -> list[dict[str, str]]:
    """Evaluate immutable acceptance data, then record every available verdict.

    Evaluation deliberately occurs outside the store lock: the pr_checks kind
    may wait for its fixed subprocess. The subsequent locked write checks that
    each item is still non-terminal before attaching its verdict.
    """
    if not isinstance(item_ids, list) or not item_ids or len(item_ids) > _MAX_ITEMS:
        raise WorkItemError("item_ids must be a non-empty bounded array")
    if not all(isinstance(item_id, str) and item_id for item_id in item_ids):
        raise WorkItemError("item_ids must contain opaque IDs")
    if len(set(item_ids)) != len(item_ids):
        raise WorkItemError("item_ids must not contain duplicates")
    cycle = active_cycle(session_key)
    if cycle is None:
        raise WorkItemError("there is no active work cycle to evaluate")
    selected = [_find_item(cycle, item_id) for item_id in item_ids]
    verdicts: dict[str, tuple[str, str]] = {}
    for item in selected:
        if item["state"] not in TERMINAL_STATES:
            try:
                verdicts[item["id"]] = evaluator({"id": item["id"], "accept": item["acceptance"]})
            except Exception:
                # One fixed evaluator defect must not hide sibling outcomes.
                # Do not include a raw exception here: it can carry a command
                # output or filesystem path that is not safe to retain in a
                # durable coordinator record.
                verdicts[item["id"]] = ("error", "acceptance evaluator failed for this item")

    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    results: list[dict[str, str]] = []
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        current = state["active_cycle"]
        if current is None or current["id"] != cycle["id"]:
            raise WorkItemError("the work cycle changed while evaluation was running")
        now = _now_iso()
        for item_id in item_ids:
            item = _find_item(current, item_id)
            if item["state"] in TERMINAL_STATES:
                results.append(
                    {"id": item_id, "verdict": "skipped", "evidence": "item is terminal"}
                )
                continue
            verdict, evidence = verdicts[item_id]
            if verdict not in {"pass", "fail", "pending", "refused", "error"}:
                raise WorkItemError("acceptance evaluator returned an unknown verdict")
            evidence_text = _text(evidence, _MAX_EVIDENCE, "evaluator evidence")
            item["last_evaluation"] = {"at": now, "verdict": verdict, "evidence": evidence_text}
            _append_event(item, kind="evaluation", text=f"{verdict}: {evidence_text}", now=now)
            if verdict == "pass":
                item["state"] = STATE_ACCEPTED
                item["finished_at"] = now
                _append_event(item, kind="state", text="acceptance evaluator passed", now=now)
            item["updated_at"] = now
            results.append({"id": item_id, "verdict": verdict, "evidence": evidence_text})
        current["updated_at"] = now
        _write_state(dir_path, key, state)
    return results



# ---------------------------------------------------------------------------
# Slice 3: launched-subagent assignment, launch receipts, worker surface.
#
# The coordinator arms one assignment per live work item. The subagent
# manager's event stream is the only delivery authority: a launch is
# ``pending_delivery`` until the spawned run reports, ``launch_queued`` once
# the manager accepted it, and ``delivered`` (item ``dispatched``) when the
# first turn starts. Receipts are keyed by the preassigned run ID so the
# hook needs nothing but the parent key and the run identity; a receipt for
# a run that is already past the receipt point is a no-op, which makes
# duplicate manager events safe.
# ---------------------------------------------------------------------------


def _render_acceptance(acceptance: dict[str, Any]) -> list[str]:
    lines = [f"  kind: {acceptance['kind']}"]
    if acceptance["kind"] == "pr_checks":
        lines.append(f"  repo: {acceptance['repo']}")
        lines.append(f"  pr: {acceptance['pr']}")
    elif acceptance["kind"] == "file":
        lines.append(f"  path: {acceptance['path']}")
        lines.append(f"  exists: {str(acceptance['exists']).lower()}")
    return lines


def render_contract(item: dict[str, Any], assignment: dict[str, Any]) -> str:
    """Render the deterministic plain-text contract handed to a launched worker.

    The contract is product text, not protocol: it names the immutable
    acceptance, the current next action, and the worker's reporting duties.
    Its SHA-256 digest is stored on the assignment, so the text itself never
    carries the digest. The attempt count stays out of the contract: a
    dispatch retry re-renders the same assignment under the same digest.
    """
    lines = [
        "KiroCrew work item contract",
        f"Assignment: {assignment['id']}",
        f"Work item: {item['id']}",
        f"Worker run: {assignment['worker_run_id']}",
        "",
        f"Title: {item['title']}",
        "",
        "Acceptance (evaluator-owned; the worker cannot change it):",
        *_render_acceptance(item["acceptance"]),
    ]
    if item["canonical_ref"]:
        lines += ["", f"Canonical ref: {item['canonical_ref']}"]
    if item["declared_resources"]:
        lines += ["", "Declared resources:"]
        lines += [f"  - {resource}" for resource in item["declared_resources"]]
    lines += [
        "",
        "Current next action:",
        item["next_action"],
        "",
        "Worker instructions:",
        "- Report progress with work_item_report_progress (kind progress or blocker).",
        "- Finish with exactly one work_item_submit_handoff carrying the outcome,",
        "  the next action for the coordinator, and the verification evidence.",
        "- If blocked, include both the blocker and its release condition.",
        "- You cannot change the item state, acceptance, or this assignment;",
        "  only the coordinator's typed evaluator accepts the item.",
    ]
    contract = "\n".join(lines) + "\n"
    if len(contract.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise WorkItemError("the rendered contract exceeds the byte limit")
    return contract


def launch_item(
    session_key: str,
    item_id: str,
    candidate_fingerprint: str = ""
) -> tuple[dict[str, Any], str]:
    """Arm a new launched-subagent assignment on one non-terminal item.

    ``candidate_fingerprint`` is the server-issued launch-candidate handle
    the coordinator selected; it is the retry target, never a worker key.
    Returns the updated item copy plus the exact contract string the
    dispatcher must hand to the subagent manager.
    """
    if candidate_fingerprint and not _FINGERPRINT_RE.fullmatch(candidate_fingerprint):
        raise WorkItemError("the launch candidate fingerprint is invalid")
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemNotFound(f"work item {item_id!r} was not found")
        item = _find_item(cycle, item_id)
        if item["state"] in TERMINAL_STATES:
            raise WorkItemError("a terminal work item cannot be launched")
        assignment = item["assignment"]
        if assignment is not None and assignment["status"] != ASSIGNMENT_REVOKED:
            raise WorkItemError("an active assignment already exists for this work item")
        now = _now_iso()
        run_id = uuid.uuid4().hex[:8]
        worker_key = f"subagent:{run_id}"
        prospective = {
            "id": _new_id("wa"),
            "source": ASSIGNMENT_SOURCE_LAUNCHED,
            "worker_session_key": worker_key,
            "worker_fingerprint": hashlib.sha256(worker_key.encode("utf-8")).hexdigest()[:16],
            "worker_slot": "",
            "worker_run_id": run_id,
            "candidate_fingerprint": candidate_fingerprint,
            "status": ASSIGNMENT_PENDING_DELIVERY,
            "attempt": 1,
            "contract_digest": "",
            "created_at": now,
            "delivered_at": "",
            "failed_at": "",
            "failure_code": "",
        }
        contract = render_contract(item, prospective)
        prospective["contract_digest"] = hashlib.sha256(contract.encode("utf-8")).hexdigest()
        item["assignment"] = prospective
        _append_event(
            item,
            kind="assignment",
            text=f"launch assigned to subagent run {run_id} (attempt 1)",
            now=now,
        )
        cycle["updated_at"] = now
        _write_state(dir_path, key, state)
        return copy.deepcopy(item), contract


def _record_receipt(
    session_key: str,
    run_id: str,
    mutate: Callable[[dict[str, Any], str, dict[str, Any]], list[tuple[str, str]] | None],
) -> None:
    """Apply one launch receipt to the item owning ``run_id``; a no-op otherwise.

    Receipts are the manager's event stream: unknown runs, closed cycles, and
    receipts that arrive after the assignment moved on must all be silent
    no-ops, never errors, because the hook cannot know which are duplicates.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise WorkItemError("invalid subagent run ID")
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            return
        now = _now_iso()
        for item in cycle["items"]:
            assignment = item["assignment"]
            if assignment is None or assignment["worker_run_id"] != run_id:
                continue
            if item["state"] in TERMINAL_STATES:
                return
            events = mutate(item, now, assignment)
            if not events:
                return
            for event_kind, event_text in events:
                _append_event(item, kind=event_kind, text=event_text, now=now)
            item["updated_at"] = now
            cycle["updated_at"] = now
            break
        else:
            return
        _write_state(dir_path, key, state)


def record_launch_accepted(session_key: str, run_id: str) -> None:
    """Mark a pending launch as accepted by the manager; duplicates are no-ops."""

    def mutate(item: dict[str, Any], now: str, assignment: dict[str, Any]):
        if assignment["status"] != ASSIGNMENT_PENDING_DELIVERY:
            return None
        assignment["status"] = ASSIGNMENT_LAUNCH_QUEUED
        return [
            (
                "assignment",
                f"launch accepted; subagent run {run_id} queued (attempt {assignment['attempt']})",
            )
        ]

    _record_receipt(session_key, run_id, mutate)


def record_launch_delivered(session_key: str, run_id: str) -> None:
    """Deliver the contract: the item is dispatched to the live subagent run."""

    def mutate(item: dict[str, Any], now: str, assignment: dict[str, Any]):
        if assignment["status"] not in {ASSIGNMENT_PENDING_DELIVERY, ASSIGNMENT_LAUNCH_QUEUED}:
            return None
        assignment["status"] = ASSIGNMENT_DELIVERED
        assignment["delivered_at"] = now
        item["state"] = STATE_DISPATCHED
        item["finished_at"] = ""
        return [
            ("assignment", f"launch delivered to subagent run {run_id}"),
            ("state", f"item dispatched to subagent run {run_id}"),
        ]

    _record_receipt(session_key, run_id, mutate)


def record_launch_failed(session_key: str, run_id: str, failure_code: str) -> None:
    """Record a launch refusal or capacity failure; the item stays open."""
    code = _text(failure_code, _MAX_FAILURE_CODE, "failure_code", required=True)

    def mutate(item: dict[str, Any], now: str, assignment: dict[str, Any]):
        if assignment["status"] not in {ASSIGNMENT_PENDING_DELIVERY, ASSIGNMENT_LAUNCH_QUEUED}:
            return None
        assignment["status"] = ASSIGNMENT_FAILED
        assignment["failed_at"] = now
        assignment["failure_code"] = code
        return [("assignment", f"launch failed for subagent run {run_id}: {code}")]

    _record_receipt(session_key, run_id, mutate)


def record_runtime_event(session_key: str, run_id: str, text: str) -> None:
    """Append one bounded runtime note for a delivered or revoked worker run."""
    event_text = _text(text, _MAX_TEXT, "text", required=True)

    def mutate(item: dict[str, Any], now: str, assignment: dict[str, Any]):
        if assignment["status"] not in {ASSIGNMENT_DELIVERED, ASSIGNMENT_REVOKED}:
            return None
        return [("runtime", event_text)]

    _record_receipt(session_key, run_id, mutate)


def arm_launch_retry(session_key: str, item_id: str) -> dict[str, Any]:
    """Re-arm an undelivered or failed launch with one higher attempt number."""
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemNotFound(f"work item {item_id!r} was not found")
        item = _find_item(cycle, item_id)
        if item["state"] in TERMINAL_STATES:
            raise WorkItemError("a terminal work item cannot be retried")
        assignment = item["assignment"]
        if assignment is None or assignment["status"] not in {
            ASSIGNMENT_PENDING_DELIVERY,
            ASSIGNMENT_FAILED,
        }:
            raise WorkItemError("the assignment is not eligible for a launch retry")
        if assignment["attempt"] >= _MAX_ATTEMPTS:
            raise WorkItemError("the assignment has exhausted its launch attempts")
        now = _now_iso()
        assignment["attempt"] += 1
        assignment["status"] = ASSIGNMENT_PENDING_DELIVERY
        assignment["failed_at"] = ""
        assignment["failure_code"] = ""
        _append_event(
            item,
            kind="assignment",
            text=(
                "launch retry armed for subagent run "
                f"{assignment['worker_run_id']} (attempt {assignment['attempt']})"
            ),
            now=now,
        )
        item["updated_at"] = now
        cycle["updated_at"] = now
        _write_state(dir_path, key, state)
        return copy.deepcopy(item)


def revoke_assignment(session_key: str, item_id: str) -> dict[str, Any]:
    """Revoke the live assignment and return the item to the coordinator."""
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemNotFound(f"work item {item_id!r} was not found")
        item = _find_item(cycle, item_id)
        if item["state"] in TERMINAL_STATES:
            raise WorkItemError("a terminal work item cannot be revoked")
        assignment = item["assignment"]
        if assignment is None:
            raise WorkItemError("the work item has no assignment to revoke")
        if assignment["status"] == ASSIGNMENT_REVOKED:
            raise WorkItemError("the assignment was already revoked")
        now = _now_iso()
        assignment["status"] = ASSIGNMENT_REVOKED
        item["state"] = STATE_PROPOSED
        item["finished_at"] = ""
        _append_event(
            item, kind="assignment", text=f"assignment {assignment['id']} revoked", now=now
        )
        item["updated_at"] = now
        cycle["updated_at"] = now
        _write_state(dir_path, key, state)
        return copy.deepcopy(item)


_WORKER_KEY_RE = re.compile(r"subagent:[0-9a-f]{8}\Z")


def _check_worker_key(worker_key: str) -> None:
    if not isinstance(worker_key, str) or not _WORKER_KEY_RE.fullmatch(worker_key):
        raise WorkItemAssignmentDenied("the caller key is not a launched-subagent key")


def _iter_store_states():
    """Yield (dir, state) for every readable coordinator store, newest first.

    This is a lockless existence scan for the worker surface: a torn read is
    treated as no-match, and the authoritative locked write re-verifies
    ownership before anything is persisted.
    """
    root = _root()
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        state_path = child / _STATE_FILE
        try:
            if state_path.stat().st_size > _MAX_STATE_BYTES * 2:
                continue
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(raw, dict):
            yield child, raw


def _find_worker_item(
    worker_key: str, item_id: str
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Locate (dir, state, item, assignment) or refuse without leaking existence."""
    _check_worker_key(worker_key)
    if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
        raise WorkItemAssignmentDenied("the work item ID is not recognized")
    for dir_path, raw in _iter_store_states():
        cycle = raw.get("active_cycle")
        if not isinstance(cycle, dict):
            continue
        items = cycle.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("id") != item_id:
                continue
            assignment = item.get("assignment")
            if isinstance(assignment, dict) and assignment.get("worker_session_key") == worker_key:
                return dir_path, raw, item, assignment
            raise WorkItemAssignmentDenied("the work item is not assigned to this worker")
    raise WorkItemAssignmentDenied("the work item is not assigned to this worker")


def _read_coordinator_key(dir_path: Path) -> str:
    key_path = dir_path / _KEY_FILE
    try:
        if key_path.stat().st_size > 4096:
            raise WorkItemStoreCorrupt("coordinator key record is oversized")
        key = key_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkItemStoreCorrupt("coordinator key record is unreadable") from exc
    if not key:
        raise WorkItemStoreCorrupt("coordinator key record is empty")
    return key


def _worker_context(
    dir_path: Path, worker_key: str, item_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """Re-verify ownership under the store lock before a worker read or write."""
    _check_worker_key(worker_key)
    coordinator_key = _read_coordinator_key(dir_path)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemAssignmentStale("the work cycle closed while the worker was acting")
        try:
            item = _find_item(cycle, item_id)
        except WorkItemNotFound as exc:
            raise WorkItemAssignmentStale("the work item is no longer active") from exc
        assignment = item["assignment"]
        if assignment is None or assignment["worker_session_key"] != worker_key:
            raise WorkItemAssignmentDenied("the work item is not assigned to this worker")
        if item["state"] in TERMINAL_STATES or assignment["status"] not in ASSIGNMENT_ACTIVE:
            raise WorkItemAssignmentStale("the assignment is no longer active")
        return state, item, assignment, coordinator_key


def _worker_view(item: dict[str, Any]) -> dict[str, Any]:
    """The worker's projection: its own assignment, never evaluator internals."""
    assignment = item["assignment"]
    return {
        "id": item["id"],
        "title": item["title"],
        "state": item["state"],
        "acceptance": copy.deepcopy(item["acceptance"]),
        "canonical_ref": item["canonical_ref"],
        "declared_resources": list(item["declared_resources"]),
        "next_action": item["next_action"],
        "assignment": {
            "id": assignment["id"],
            "status": assignment["status"],
            "attempt": assignment["attempt"],
            "contract_digest": assignment["contract_digest"],
        },
        "worker_handoff": copy.deepcopy(item["worker_handoff"]),
        "events": copy.deepcopy(item["events"]),
        "updated_at": item["updated_at"],
    }


def worker_assigned_list(worker_key: str) -> list[dict[str, Any]]:
    """List this worker's live assignments without naming other workers' items."""
    _check_worker_key(worker_key)
    views: list[dict[str, Any]] = []
    for _dir_path, raw in _iter_store_states():
        cycle = raw.get("active_cycle")
        if not isinstance(cycle, dict):
            continue
        items = cycle.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            assignment = item.get("assignment")
            if not isinstance(assignment, dict):
                continue
            if assignment.get("worker_session_key") != worker_key:
                continue
            if assignment.get("status") not in ASSIGNMENT_ACTIVE:
                continue
            if item.get("state") in TERMINAL_STATES:
                continue
            try:
                views.append(_worker_view(_coerce_item(item)))
            except WorkItemStoreCorrupt:
                continue
    return views


def worker_assigned_read(worker_key: str, item_id: str) -> dict[str, Any]:
    """Read one live assignment exactly as the worker sees it."""
    dir_path, _raw, _item, _assignment = _find_worker_item(worker_key, item_id)
    _state, item, _assignment, _key = _worker_context(dir_path, worker_key, item_id)
    return _worker_view(item)


def worker_report_progress(
    worker_key: str, item_id: str, text: str, kind: str
) -> dict[str, Any]:
    """Append one worker progress or blocker note under its worker fingerprint."""
    if kind not in {"progress", "blocker"}:
        raise WorkItemError("progress kind must be progress or blocker")
    event_text = _text(text, _MAX_TEXT, "text", required=True)
    dir_path, _raw, _item, _assignment = _find_worker_item(worker_key, item_id)
    state, item, assignment, coordinator_key = _worker_context(dir_path, worker_key, item_id)
    now = _now_iso()
    _append_event(
        item,
        kind=kind,
        text=event_text,
        now=now,
        actor=assignment["worker_fingerprint"],
    )
    item["updated_at"] = now
    state["active_cycle"]["updated_at"] = now
    _write_state(dir_path, coordinator_key, state)
    return _worker_view(item)


def _normalize_handoff_input(
    outcome: Any,
    next_action: Any,
    verification: Any,
    canonical_ref: Any,
    blocker: Any,
    release_condition: Any,
) -> tuple[str, str, list[str], str, str, str]:
    outcome_text = _text(outcome, _MAX_TEXT, "outcome", required=True)
    action_text = _text(next_action, _MAX_TEXT, "next_action", required=True)
    if (
        not isinstance(verification, list)
        or not verification
        or len(verification) > _MAX_VERIFICATION
        or not all(isinstance(entry, str) and entry.strip() for entry in verification)
    ):
        raise WorkItemError("verification must be 1-8 non-empty strings")
    verification_text = [
        _text(entry, _MAX_VERIFICATION_ENTRY, "verification entry", required=True)
        for entry in verification
    ]
    ref_text = _text(canonical_ref, _MAX_REF, "canonical_ref")
    blocker_text = _text(blocker, _MAX_TEXT, "blocker")
    release_text = _text(release_condition, _MAX_TEXT, "release_condition")
    if bool(blocker_text) != bool(release_text):
        raise WorkItemError("blocker and release_condition must both be set or both be empty")
    return outcome_text, action_text, verification_text, ref_text, blocker_text, release_text


def worker_submit_handoff(
    worker_key: str,
    item_id: str,
    *,
    outcome: str,
    next_action: str,
    verification: list[str],
    canonical_ref: str = "",
    blocker: str = "",
    release_condition: str = "",
) -> dict[str, Any]:
    """Record the worker's final handoff; the item state is never changed here.

    Acceptance stays with the coordinator's typed evaluator; the handoff only
    replaces the latest handoff record and appends one summary event.
    """
    (
        outcome_text,
        action_text,
        verification_text,
        ref_text,
        blocker_text,
        release_text,
    ) = _normalize_handoff_input(
        outcome, next_action, verification, canonical_ref, blocker, release_condition
    )
    dir_path, _raw, _item, _assignment = _find_worker_item(worker_key, item_id)
    state, item, assignment, coordinator_key = _worker_context(dir_path, worker_key, item_id)
    now = _now_iso()
    fingerprint = assignment["worker_fingerprint"]
    item["worker_handoff"] = {
        "outcome": outcome_text,
        "canonical_ref": ref_text,
        "next_action": action_text,
        "verification": verification_text,
        "blocker": blocker_text,
        "release_condition": release_text,
        "at": now,
        "actor": fingerprint,
    }
    _append_event(
        item,
        kind="worker_handoff",
        text=outcome_text,
        now=now,
        actor=fingerprint,
    )
    item["updated_at"] = now
    state["active_cycle"]["updated_at"] = now
    _write_state(dir_path, coordinator_key, state)
    return _worker_view(item)


def _archive_path(dir_path: Path, cycle_id: str) -> Path:
    if not isinstance(cycle_id, str) or not _CYCLE_ID_RE.fullmatch(cycle_id):
        raise WorkItemError("invalid work cycle ID")
    return _archive_dir(dir_path) / f"{cycle_id}.json"


def _read_archive(path: Path) -> dict[str, Any]:
    try:
        raw = _read_json(path, "work-cycle archive", max_bytes=_MAX_ARCHIVE_BYTES)
    except FileNotFoundError as exc:
        raise WorkItemNotFound(f"work-cycle archive {path.stem!r} was not found") from exc
    if not isinstance(raw, dict):
        raise WorkItemStoreCorrupt("work-cycle archive has invalid fields")
    if raw.get("schema") != SCHEMA_VERSION or isinstance(raw.get("schema"), bool):
        raise WorkItemStoreCorrupt("work-cycle archive has an invalid schema")
    cycle = _coerce_cycle(raw.get("cycle"))
    if cycle is None:
        raise WorkItemStoreCorrupt("work-cycle archive has no cycle")
    if not all(
        isinstance(raw.get(field), str) for field in ("summary", "closed_at", "source_cycle_digest")
    ):
        raise WorkItemStoreCorrupt("work-cycle archive has invalid fields")
    return {
        "schema": raw.get("schema"),
        "cycle": cycle,
        "summary": raw["summary"],
        "closed_at": raw["closed_at"],
        "source_cycle_digest": raw["source_cycle_digest"],
    }


def _archive_usage(dir_path: Path) -> tuple[int, int]:
    archive = _archive_dir(dir_path)
    try:
        files = [path for path in archive.glob("*.json") if path.is_file()]
        return len(files), sum(path.stat().st_size for path in files)
    except OSError as exc:
        raise WorkItemStoreCorrupt("work-cycle archive cannot be inspected") from exc


def close_cycle(session_key: str, *, summary: str) -> dict[str, Any]:
    """Archive an all-terminal active cycle, then clear it recoverably."""
    key = ledger_key(session_key)
    dir_path = coordinator_dir(key)
    summary_text = _text(summary, _MAX_TEXT, "summary", required=True)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        _migrate_legacy_unlocked(dir_path, key, state)
        state = _read_state_unlocked(dir_path)
        cycle = state["active_cycle"]
        if cycle is None:
            raise WorkItemError("there is no active work cycle to close")
        if any(item["state"] not in TERMINAL_STATES for item in cycle["items"]):
            raise WorkItemError("every work item must be terminal before closing the cycle")
        archive = _write_archive_unlocked(dir_path, cycle, summary=summary_text)
        state["active_cycle"] = None
        _write_state(dir_path, key, state)
        return _archive_summary(archive)


def _archive_summary(archive: dict[str, Any]) -> dict[str, Any]:
    cycle = archive["cycle"]
    return {
        "id": cycle["id"],
        "goal": cycle["goal"],
        "closed_at": archive.get("closed_at", ""),
        "summary": archive.get("summary", ""),
        "item_states": {item["id"]: item["state"] for item in cycle.get("items", [])},
    }


def list_archives(session_key: str) -> list[dict[str, Any]]:
    """List immutable closed-cycle summaries after the one-time legacy import."""
    ensure_migrated(session_key)
    dir_path = coordinator_dir(session_key)
    archive_dir = _archive_dir(dir_path)
    try:
        paths = sorted(archive_dir.glob("*.json")) if archive_dir.is_dir() else []
    except OSError as exc:
        raise WorkItemStoreCorrupt("work-cycle archive cannot be listed") from exc
    archives = [_archive_summary(_read_archive(path)) for path in paths]
    return sorted(archives, key=lambda archive: str(archive["closed_at"]), reverse=True)


def read_archive(session_key: str, cycle_id: str) -> dict[str, Any]:
    """Read one immutable closed-cycle archive."""
    ensure_migrated(session_key)
    return copy.deepcopy(_read_archive(_archive_path(coordinator_dir(session_key), cycle_id)))


def purge(session_key: str) -> None:
    """Remove one coordinator's active and archived work-item state."""
    try:
        directory = coordinator_dir(session_key)
    except WorkItemError:
        return
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        return


def purge_matching(exact_keys: set[str], folded_keys: set[str], fold: Any) -> int:
    """Purge stores whose durable breadcrumb matches a permanent session delete."""
    try:
        children = list(_root().iterdir())
    except FileNotFoundError:
        return 0
    removed = 0
    for child in children:
        try:
            if not child.is_dir():
                continue
            key = (child / _KEY_FILE).read_text(encoding="utf-8").strip()
            if key in exact_keys or fold(key) in folded_keys:
                shutil.rmtree(child)
                removed += 1
        except (OSError, UnicodeDecodeError):
            logger.warning(
                "work-item purge sweep skipped unreadable store %s", child, exc_info=True
            )
            continue
    return removed
