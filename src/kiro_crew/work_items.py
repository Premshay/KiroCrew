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
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_ACCEPTED, STATE_REJECTED, STATE_CANCELLED})
COORDINATOR_TRANSITIONS = frozenset(
    {STATE_PROPOSED, STATE_WAITING, STATE_REJECTED, STATE_CANCELLED}
)

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
_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

_EVENT_KINDS = frozenset({"created", "progress", "blocker", "state", "evaluation"})
_LEGACY_ITEM_KEY_RE = re.compile(r"item-[0-9]+\Z")
_LEGACY_STATUSES = frozenset({"running", "waiting", "pass", "fail"})
_ITEM_ID_RE = re.compile(r"wi_[0-9a-f]{32}\Z")
_CYCLE_ID_RE = re.compile(r"wc_[0-9a-f]{32}\Z")

logger = logging.getLogger(__name__)


class WorkItemError(ValueError):
    """Base error for a rejected work-item operation."""


class WorkItemNotFound(WorkItemError):
    """The requested item or archive does not exist."""


class WorkItemStoreCorrupt(WorkItemError):
    """Persisted work-item data cannot safely be interpreted."""


class WorkItemArchiveFull(WorkItemError):
    """Closing would exceed the store's deliberate archive retention bound."""


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
    return {"ts": ts, "kind": kind, "text": text}


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
    if raw["state"] not in TERMINAL_STATES | {STATE_PROPOSED, STATE_WAITING}:
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


def _append_event(item: dict[str, Any], *, kind: str, text: str, now: str) -> None:
    if kind not in _EVENT_KINDS:
        raise WorkItemError("work-item event kind is not supported")
    item["events"] = (list(item["events"]) + [{"ts": now, "kind": kind, "text": text}])[
        -_MAX_ITEM_EVENTS:
    ]


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
