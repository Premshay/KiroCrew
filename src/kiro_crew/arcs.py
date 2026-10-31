"""Durable per-arc board: a typed, append-only entry log shared by an arc's members.

Layout under ``data_home()/arcs/<arc_id>/``: ``arc.json`` (atomic write),
``entries.jsonl`` (appended under the directory lock) and ``cursors.json``
(member key to last seen entry id). All state is on disk; nothing is cached.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import release_lock, try_acquire_lock
from kiro_crew.session_ledger import ledger_key

KINDS = ("CLAIM", "FACT", "FAIL", "ASK", "DONE")
OWNER = "owner"

_MAX_TEXT = 100
_MAX_DONE_TEXT = 300
_MAX_STEP = 100
_MAX_CHECKED = 8
_MAX_CHECKED_ITEM = 200
_MAX_FIELD = 2_000
_ARC_ID_RE = re.compile(r"arc_[0-9a-f]{32}\Z")
_ENTRY_ID_RE = re.compile(r"ae_[0-9a-f]{32}\Z")
_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

logger = logging.getLogger(__name__)


class ArcError(ValueError):
    """A rejected arc operation."""


class ArcNotFound(ArcError):
    """The arc does not exist."""


class ArcAccessDenied(ArcError):
    """The caller is not a member of the arc."""


def _now() -> datetime:
    return datetime.now().astimezone()


def _root() -> Path:
    return data_home() / "arcs"


def _arc_dir(arc_id: str) -> Path:
    if not isinstance(arc_id, str) or not _ARC_ID_RE.fullmatch(arc_id):
        raise ArcNotFound(f"arc {arc_id!r} was not found")
    return _root() / arc_id


@contextmanager
def _locked(dir_path: Path) -> Iterator[None]:
    if not dir_path.is_dir():
        raise ArcNotFound(f"arc {dir_path.name!r} was not found")
    fd = os.open(str(dir_path / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
        while not try_acquire_lock(fd, exclusive=True):
            if time.monotonic() >= deadline:
                raise OSError("arc store lock is held; try again")
            time.sleep(_LOCK_POLL_SECS)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", mode=0o600)


def _load_arc(dir_path: Path) -> dict[str, Any]:
    try:
        return json.loads((dir_path / "arc.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ArcNotFound(f"arc {dir_path.name!r} was not found") from None


def _load_cursors(dir_path: Path) -> dict[str, str]:
    try:
        return json.loads((dir_path / "cursors.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _parse_entry(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("entry line is not an object")
    return value


def _load_entries(dir_path: Path) -> list[dict[str, Any]]:
    try:
        data = (dir_path / "entries.jsonl").read_bytes()
    except FileNotFoundError:
        return []
    lines = [line for line in data.split(b"\n") if line.strip()]
    entries = []
    for index, line in enumerate(lines):
        try:
            entries.append(_parse_entry(line))
        except ValueError:
            if index != len(lines) - 1:
                raise
            logger.warning("ignoring torn trailing line in %s", dir_path / "entries.jsonl")
    return entries


def _drop_torn_tail(path: Path) -> None:
    """Repair an unterminated final line: keep it if it is a whole entry, else truncate."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    if not data or data.endswith(b"\n"):
        return
    start = data.rfind(b"\n") + 1
    with open(path, "r+b") as handle:
        try:
            _parse_entry(data[start:])
        except ValueError:
            handle.truncate(start)
        else:
            handle.seek(0, os.SEEK_END)
            handle.write(b"\n")


def _str(value: Any, limit: int, field: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ArcError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ArcError(f"{field} must not be empty")
    if len(result) > limit:
        raise ArcError(f"{field} exceeds {limit} characters")
    return result


def _str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ArcError(f"{field} must be a list of strings")
    return [_str(item, _MAX_FIELD, field, required=True) for item in value]


def create_arc(spec: dict[str, Any]) -> dict[str, Any]:
    """Create an arc from its spec and return it."""
    if not isinstance(spec, dict):
        raise ArcError("spec must be an object")
    autonomy = spec.get("autonomy", 1)
    if autonomy not in (1, 2, 3) or isinstance(autonomy, bool):
        raise ArcError("autonomy must be 1, 2 or 3")
    milestones = spec.get("milestones") or []
    if not isinstance(milestones, list):
        raise ArcError("milestones must be a list")
    arc = {
        "id": f"arc_{uuid.uuid4().hex}",
        "title": _str(spec.get("title"), _MAX_FIELD, "title", required=True),
        "goal": _str(spec.get("goal"), _MAX_FIELD, "goal"),
        "reason": _str(spec.get("reason"), _MAX_FIELD, "reason"),
        "lineage": _str(spec.get("lineage"), _MAX_FIELD, "lineage"),
        "source": _str(spec.get("source"), _MAX_FIELD, "source"),
        "autonomy": autonomy,
        "milestones": [
            {
                "name": _str(
                    m.get("name") if isinstance(m, dict) else None, 200, "name", required=True
                ),
                "criteria": _str(
                    m.get("criteria") if isinstance(m, dict) else None, _MAX_FIELD, "criteria"
                ),
            }
            for m in milestones
        ],
        "decisions": _str_list(spec.get("decisions"), "decisions"),
        "rules": _str_list(spec.get("rules"), "rules"),
        "reuse": _str_list(spec.get("reuse"), "reuse"),
        "channel_id": _str(spec.get("channel_id"), 512, "channel_id"),
        "members": {},
        "milestone_index": 0,
        "created_at": _now().isoformat(timespec="seconds"),
    }
    dir_path = _arc_dir(arc["id"])
    dir_path.mkdir(parents=True, exist_ok=True)
    with _locked(dir_path):
        _write_json(dir_path / "arc.json", arc)
    return copy.deepcopy(arc)


def read_arc(arc_id: str) -> dict[str, Any]:
    """Return one arc."""
    dir_path = _arc_dir(arc_id)
    if not dir_path.is_dir():
        raise ArcNotFound(f"arc {arc_id!r} was not found")
    with _locked(dir_path):
        return _load_arc(dir_path)


def list_arcs() -> list[dict[str, Any]]:
    """Return every arc, oldest first."""
    root = _root()
    if not root.is_dir():
        return []
    found = [
        read_arc(p.name)
        for p in root.iterdir()
        if _ARC_ID_RE.fullmatch(p.name) and (p / "arc.json").is_file()
    ]
    return sorted(found, key=lambda arc: (arc["created_at"], arc["id"]))


def arcs_for_member(session_key: str) -> list[dict[str, Any]]:
    """Return the arcs the session is a member of."""
    key = ledger_key(session_key)
    return [arc for arc in list_arcs() if key in arc["members"]]


def add_member(arc_id: str, session_key: str, role: str) -> dict[str, Any]:
    """Add (or re-role) a member and return the arc."""
    key = ledger_key(session_key)
    if not key or key == OWNER:
        raise ArcError("invalid member session key")
    dir_path = _arc_dir(arc_id)
    with _locked(dir_path):
        arc = _load_arc(dir_path)
        arc["members"][key] = {
            "role": _str(role, 200, "role"),
            "joined_at": arc["members"].get(key, {}).get("joined_at")
            or _now().isoformat(timespec="seconds"),
        }
        _write_json(dir_path / "arc.json", arc)
        return arc


def accept_milestone(arc_id: str) -> dict[str, Any]:
    """Advance the arc to its next milestone and return the arc."""
    dir_path = _arc_dir(arc_id)
    with _locked(dir_path):
        arc = _load_arc(dir_path)
        arc["milestone_index"] += 1
        _write_json(dir_path / "arc.json", arc)
        return arc


def _validate_entry(entry: dict[str, Any], members: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ArcError("entry must be an object")
    kind = entry.get("kind")
    if kind not in KINDS:
        raise ArcError(f"kind must be one of {', '.join(KINDS)}")
    limit = _MAX_DONE_TEXT if kind == "DONE" else _MAX_TEXT
    clean: dict[str, Any] = {
        "kind": kind,
        "text": _str(entry.get("text"), limit, "text", required=True),
        "link": _str(entry.get("link"), _MAX_FIELD, "link"),
        "reply_to": _str(entry.get("reply_to"), 64, "reply_to"),
    }
    if kind == "CLAIM":
        clean["step"] = _str(entry.get("step"), _MAX_STEP, "step", required=True)
        checked = entry.get("checked")
        if not isinstance(checked, list) or not 1 <= len(checked) <= _MAX_CHECKED:
            raise ArcError(f"checked must list 1-{_MAX_CHECKED} decision records or components")
        clean["checked"] = [_str(c, _MAX_CHECKED_ITEM, "checked", required=True) for c in checked]
    else:
        clean["step"] = _str(entry.get("step"), _MAX_STEP, "step")
    if kind == "ASK":
        to = _str(entry.get("to"), 512, "to", required=True)
        to = to if to == OWNER else ledger_key(to)
        if to != OWNER and to not in members:
            raise ArcError("to must be a member key or owner")
        clean["to"] = to
    if kind == "DONE" and not clean["link"]:
        raise ArcError("DONE requires a link")
    if clean["reply_to"] and not _ENTRY_ID_RE.fullmatch(clean["reply_to"]):
        raise ArcError("reply_to must be an entry id")
    return clean


def append_entry(arc_id: str, author: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Validate and append one entry authored by a member or the owner."""
    key = author if author == OWNER else ledger_key(author)
    if key == OWNER and author != OWNER:
        raise ArcAccessDenied("only the owner may author as owner")
    dir_path = _arc_dir(arc_id)
    with _locked(dir_path):
        arc = _load_arc(dir_path)
        if key != OWNER and key not in arc["members"]:
            raise ArcAccessDenied(f"{author!r} is not a member of arc {arc_id}")
        clean = _validate_entry(entry, arc["members"])
        milestone = entry.get("milestone", arc["milestone_index"])
        if not isinstance(milestone, int) or isinstance(milestone, bool) or milestone < 0:
            raise ArcError("milestone must be a non-negative integer")
        stored = {
            **clean,
            "id": f"ae_{uuid.uuid4().hex}",
            "ts": _now().isoformat(timespec="seconds"),
            "author": key,
            "milestone": milestone,
        }
        line = json.dumps(stored, ensure_ascii=False, sort_keys=True) + "\n"
        _drop_torn_tail(dir_path / "entries.jsonl")
        with open(dir_path / "entries.jsonl", "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return stored


def _entries(arc_id: str) -> list[dict[str, Any]]:
    dir_path = _arc_dir(arc_id)
    if not dir_path.is_dir():
        raise ArcNotFound(f"arc {arc_id!r} was not found")
    with _locked(dir_path):
        _load_arc(dir_path)
        return _load_entries(dir_path)


def read_entries(
    arc_id: str, *, after: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Return up to ``limit`` entries, optionally only those after entry id ``after``."""
    entries = _entries(arc_id)
    if after:
        ids = [e["id"] for e in entries]
        entries = entries[ids.index(after) + 1 :] if after in ids else entries
    return entries[: max(limit, 0)]


def grep_entries(arc_id: str, query: str) -> list[dict[str, Any]]:
    """Case-insensitive search: ``,`` separates OR groups, ``&`` joins AND terms."""
    groups = [
        [t.strip().casefold() for t in group.split("&") if t.strip()] for group in query.split(",")
    ]
    groups = [g for g in groups if g]
    if not groups:
        return []
    hits = []
    for entry in _entries(arc_id):
        hay = " ".join(
            str(entry.get(f, "")) for f in ("text", "step", "link", "kind", "author")
        ).casefold()
        if any(all(term in hay for term in group) for group in groups):
            hits.append(entry)
    return hits


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def open_claims(arc_id: str) -> list[dict[str, Any]]:
    """CLAIMs whose author has not yet answered with DONE or FAIL."""
    entries = _entries(arc_id)
    closed = {
        (e["author"], e["reply_to"])
        for e in entries
        if e["kind"] in ("DONE", "FAIL") and e["reply_to"]
    }
    return [e for e in entries if e["kind"] == "CLAIM" and (e["author"], e["id"]) not in closed]


def open_asks(arc_id: str) -> list[dict[str, Any]]:
    """ASKs no entry has replied to, each with ``age_secs``."""
    entries = _entries(arc_id)
    replied = {e["reply_to"] for e in entries if e["reply_to"]}
    now = _now()
    return [
        {**e, "age_secs": max(0, int((now - _parse_ts(e["ts"])).total_seconds()))}
        for e in entries
        if e["kind"] == "ASK" and e["id"] not in replied
    ]


def _normalize_step(step: str) -> str:
    return re.sub(r"\s+", " ", step.casefold()).strip()


def claim_collisions(arc_id: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Open CLAIMs by another author on the same milestone and normalized step.

    The candidate's author is read from ``entry["author"]``; without one, every
    open matching claim counts.
    """
    if entry.get("kind") != "CLAIM":
        return []
    author = entry.get("author")
    author = author if not author or author == OWNER else ledger_key(author)
    milestone = entry.get("milestone")
    if milestone is None:
        milestone = read_arc(arc_id)["milestone_index"]
    step = _normalize_step(str(entry.get("step") or ""))
    return [
        c
        for c in open_claims(arc_id)
        if c["author"] != author
        and c["milestone"] == milestone
        and _normalize_step(c["step"]) == step
    ]


def mark_seen(arc_id: str, session_key: str, entry_id: str) -> None:
    """Advance a member's cursor to ``entry_id``; it never moves backwards."""
    key = ledger_key(session_key)
    dir_path = _arc_dir(arc_id)
    with _locked(dir_path):
        arc = _load_arc(dir_path)
        if key not in arc["members"]:
            raise ArcAccessDenied(f"{session_key!r} is not a member of arc {arc_id}")
        ids = [e["id"] for e in _load_entries(dir_path)]
        if entry_id not in ids:
            raise ArcError(f"entry {entry_id!r} was not found")
        cursors = _load_cursors(dir_path)
        current = cursors.get(key)
        if current in ids and ids.index(current) >= ids.index(entry_id):
            return
        cursors[key] = entry_id
        _write_json(dir_path / "cursors.json", cursors)


def unseen_entries(arc_id: str, session_key: str, limit: int = 20) -> list[dict[str, Any]]:
    """Entries by other authors after the member's cursor, oldest first."""
    key = ledger_key(session_key)
    dir_path = _arc_dir(arc_id)
    with _locked(dir_path):
        _load_arc(dir_path)
        entries = _load_entries(dir_path)
        cursor = _load_cursors(dir_path).get(key)
    ids = [e["id"] for e in entries]
    start = ids.index(cursor) + 1 if cursor in ids else 0
    return [e for e in entries[start:] if e["author"] != key][: max(limit, 0)]


def seen_by(arc_id: str, entry_id: str) -> list[str]:
    """Member keys whose cursor is at or past ``entry_id``."""
    dir_path = _arc_dir(arc_id)
    with _locked(dir_path):
        _load_arc(dir_path)
        ids = [e["id"] for e in _load_entries(dir_path)]
        cursors = _load_cursors(dir_path)
    if entry_id not in ids:
        return []
    at = ids.index(entry_id)
    return sorted(k for k, c in cursors.items() if c in ids and ids.index(c) >= at)
