"""Subscription headroom per vendor, from the signals each harness already emits.

``spawn.route`` (``decisions/points/spawn_route.py``) skips a vendor near or past
its window before a spawn fails on it. Each reader follows the same contract:

1. its reader fills ONE normalized shape (:mod:`kiro_crew.quota_rules`): vendor ->
   accounts -> windows or models -> ``{used_pct, resets_at, as_of, status}``;
2. :mod:`kiro_crew.quota_rules` judges it with the one rule set (``low_pct``,
   ``out_pct``, ``rejected`` is out; a vendor is out only when every account is),
   giving the health shape spawn.route reads::

    {"out": bool, "used_pct": 0..100, "window": str, "resets_at": epoch s,
     "source": str, "as_of": epoch s}

``used_pct`` is the binding window's (the most-used one when an account has
several; the best account's when a vendor has several). An unknown value is an
ABSENT key, never 0: a vendor nobody has read is usable, not idle. Until a harness
says which account served a turn, every reading is account
:data:`~kiro_crew.quota_rules.DEFAULT_ACCOUNT`.

Sources
-------
- Claude: claude-agent-acp forwards the SDK's ``rate_limit_event`` as
  ``_meta["_claude/rateLimit"]`` (``SDKRateLimitInfo {status, utilization,
  rateLimitType, resetsAt}``) on a ``usage_update``. :func:`note_usage_meta` is
  called where usage frames are parsed; the newest reading per window is kept
  in memory and in ``vendor_limits.json`` under the data home, so a restart
  keeps it. ``status: rejected`` is out.
- Codex: the ``limit_id: "codex"`` row of the rollout logs
  (``dashboard/handlers/codex_usage.py``).
- Antigravity: ``antigravity_quota.json`` under the data home, written by the
  Companion: ``{as_of, models: {<id>: {remaining_fraction, reset_time, group}}}``.
- Local: ``GET /fleet/status`` of the fleet router (a role's lane is up). Lanes,
  not a subscription window, so it has no quota shape.
- Quota outs: a run that HIT its vendor's quota marks it out until its reset at
  once (:func:`note_quota_out`, called by ``subagent_manager.quota_switch``), so no
  new spawn waits for a probe or a tombstone scan to learn it. A mark is keyed by
  vendor + account + model group: a vendor whose quota is held per model group
  (:data:`PER_MODEL_VENDORS`) marks only the group, every other vendor the whole
  account. Kept in ``vendor_limits.json`` under ``_marks``.

Every reader here never raises: a missing or malformed source is "unknown".
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from kiro_crew import quota_rules as qr
from kiro_crew.quota_rules import DEFAULT_ACCOUNT

logger = logging.getLogger(__name__)

CLAUDE_RATE_LIMIT_META = "_claude/rateLimit"
STORE_NAME = "vendor_limits.json"
ANTIGRAVITY_QUOTA_NAME = "antigravity_quota.json"
FLEET_TIMEOUT_S = 1.5
#: The default "low" threshold, kept for callers of the old name. The thresholds in
#: force are :func:`kiro_crew.quota_rules.rules` (``decisions.quota_limits``).
LOW_PCT = qr.QUOTA_LOW_PCT_DEFAULT
#: Vendors whose quota is held per model group, so a quota hit marks only the group.
PER_MODEL_VENDORS = ("antigravity",)

#: Store keys that are not a vendor's default-account readings.
ACCOUNTS_KEY = "_accounts"  # {vendor: {account: {window: reading}}}, non-default accounts
MARKS_KEY = "_marks"  # {vendor: {account: {group: mark}}}, group "" = the whole account
LEGACY_OUTS_KEY = "_outs"  # before 2026-11-21: {vendor: {"vendor": mark, "models": {id: mark}}}

_lock = threading.Lock()
#: ``{vendor: {window: reading}}`` for the default account, plus the reserved keys
#: above; loaded from disk on first use.
_store: dict[str, Any] | None = None


def _data_home() -> Path:
    from kiro_crew.config.paths import peek_data_home

    return peek_data_home()


def _store_path() -> Path:
    return _data_home() / STORE_NAME


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _pct(utilization: object) -> float | None:
    """The SDK's ``utilization`` as 0..100. A fraction (<= 1) is scaled; a percentage is kept."""
    u = _finite(utilization)
    if u is None or u < 0:
        return None
    return round(min(u * 100.0 if u <= 1.0 else u, 100.0), 1)


def _merge_mark(bucket: dict[str, Any], group: str, mark: Mapping[str, Any]) -> None:
    """Keep *mark* under *group* unless the one there already lasts as long: a mark extends, never shortens."""
    prev = bucket.get(group) or {}
    if float(prev.get("until") or 0) < float(mark.get("until") or 0):
        bucket[group] = dict(mark)


def _migrate_legacy_outs(store: dict[str, Any]) -> None:
    """Fold a pre-account ``_outs`` table into ``_marks`` under the default account."""
    legacy = store.pop(LEGACY_OUTS_KEY, None)
    if not isinstance(legacy, dict):
        return
    marks = store.setdefault(MARKS_KEY, {})
    for vendor, row in legacy.items():
        if not isinstance(vendor, str) or not isinstance(row, dict):
            continue
        bucket = marks.setdefault(vendor, {}).setdefault(DEFAULT_ACCOUNT, {})
        whole = row.get("vendor")
        if isinstance(whole, dict):
            _merge_mark(bucket, "", whole)
        for model, mark in (row.get("models") or {}).items():
            if isinstance(model, str) and isinstance(mark, dict):
                _merge_mark(bucket, model, {**mark, "models": [model]})


def _load() -> dict[str, Any]:
    global _store
    if _store is not None:
        return _store
    loaded: dict[str, Any] = {}
    try:
        raw = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                if key.startswith("_"):
                    loaded[key] = json.loads(json.dumps(value))
                else:
                    loaded[key] = {str(w): dict(r) for w, r in value.items() if isinstance(r, dict)}
        _migrate_legacy_outs(loaded)
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("vendor_limits: store unreadable", exc_info=True)
    _store = loaded
    return _store


def _persist(snapshot: Mapping[str, Any]) -> None:
    try:
        from kiro_crew.atomic_write import atomic_write

        atomic_write(_store_path(), json.dumps(snapshot, sort_keys=True), restrict_to_owner=True)
    except Exception:
        logger.debug("vendor_limits: store not written", exc_info=True)


def _windows_of(store: dict[str, Any], vendor: str, account: str) -> dict[str, Any]:
    """The (mutable) ``{window: reading}`` table of *vendor*'s *account* in *store*."""
    if account == DEFAULT_ACCOUNT:
        return store.setdefault(vendor, {})
    return store.setdefault(ACCOUNTS_KEY, {}).setdefault(vendor, {}).setdefault(account, {})


def note_claude_rate_limit(
    info: object, *, now: float | None = None, account: str = DEFAULT_ACCOUNT
) -> bool:
    """Keep one ``SDKRateLimitInfo`` for *account*; ``True`` when it changed the store. Never raises."""
    if not isinstance(info, dict):
        return False
    status = info.get("status")
    if status not in ("allowed", "allowed_warning", "rejected"):
        return False
    window = info.get("rateLimitType")
    window = window if isinstance(window, str) and window else "unknown"
    reading: dict[str, Any] = {
        "status": status,
        "as_of": float(time.time() if now is None else now),
    }
    pct = _pct(info.get("utilization"))
    if pct is not None:
        reading["used_pct"] = pct
    resets = _finite(info.get("resetsAt"))
    if resets is not None:
        reading["resets_at"] = resets
    account = account or DEFAULT_ACCOUNT
    with _lock:
        store = _load()
        windows = _windows_of(store, "claude", account)
        prev = windows.get(window)
        same = prev is not None and all(
            prev.get(k) == reading.get(k) for k in ("status", "used_pct", "resets_at")
        )
        windows[window] = reading
        snapshot = json.loads(json.dumps(store))
    if not same:
        _persist(snapshot)
    return not same


def note_usage_meta(update: object) -> None:
    """Pick the Claude rate-limit reading off a ``usage_update``'s ``_meta``, if it carries one."""
    try:
        if not isinstance(update, dict):
            return
        meta = update.get("_meta")
        if isinstance(meta, dict) and CLAUDE_RATE_LIMIT_META in meta:
            note_claude_rate_limit(meta[CLAUDE_RATE_LIMIT_META])
    except Exception:
        logger.debug("vendor_limits: usage meta ignored", exc_info=True)


# ---------------------------------------------------------------------------
# Readers: each fills the normalized shape (quota_rules) and nothing else
# ---------------------------------------------------------------------------


def claude_quota() -> dict[str, Any]:
    """Claude's readings in the normalized shape: every account's windows as kept."""
    with _lock:
        store = _load()
        accounts: dict[str, Any] = {}
        default = store.get("claude")
        if isinstance(default, dict) and default:
            accounts[DEFAULT_ACCOUNT] = {"windows": json.loads(json.dumps(default))}
        for acct, windows in ((store.get(ACCOUNTS_KEY) or {}).get("claude") or {}).items():
            if isinstance(windows, dict) and windows:
                accounts[str(acct)] = {"windows": json.loads(json.dumps(windows))}
    return {"source": "claude_rate_limit_event", "accounts": accounts} if accounts else {}


def codex_quota(reading: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Codex's rollout reading (``codex_usage``) in the normalized shape; stale windows dropped."""
    if reading is None:
        try:
            from kiro_crew.dashboard.handlers.codex_usage import read_codex_rate_limits

            reading = read_codex_rate_limits()
        except Exception:
            logger.debug("vendor_limits: codex reading failed", exc_info=True)
            return {}
    if not reading:
        return {}
    as_of = _iso_ts(reading.get("captured_at")) or _finite(reading.get("source_mtime"))
    windows: dict[str, Any] = {}
    for name in ("five_hour", "weekly"):
        w = reading.get(name)
        if not isinstance(w, Mapping) or w.get("stale"):
            continue
        used = _finite(w.get("used_percent"))
        if used is None:
            continue
        row: dict[str, Any] = {"used_pct": round(used, 1)}
        if w.get("resets_at") is not None:
            row["resets_at"] = w["resets_at"]
        if as_of is not None:
            row["as_of"] = as_of
        windows[name] = row
    if not windows:
        return {}
    return {"source": "codex_rollout", "accounts": {DEFAULT_ACCOUNT: {"windows": windows}}}


def antigravity_quota(*, path: Path | None = None) -> dict[str, Any]:
    """The companion's per-model probe in the normalized shape (every row, expired or not)."""
    try:
        raw = json.loads((path or _data_home() / ANTIGRAVITY_QUOTA_NAME).read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("vendor_limits: antigravity quota unreadable", exc_info=True)
        return {}
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        return {}
    as_of = _iso_ts(raw.get("as_of"))
    models: dict[str, Any] = {}
    for mid, row in rows.items():
        if not isinstance(mid, str) or not isinstance(row, dict):
            continue
        rem = _finite(row.get("remaining_fraction"))
        if rem is None:
            continue
        entry: dict[str, Any] = {"used_pct": round(100.0 * (1.0 - min(max(rem, 0.0), 1.0)), 1)}
        reset = _iso_ts(row.get("reset_time"))
        if reset is not None:
            entry["resets_at"] = reset
        if as_of is not None:
            entry["as_of"] = as_of
        group = row.get("group")
        if isinstance(group, str) and group:
            entry["group"] = group
        models[mid] = entry
    if not models:
        return {}
    return {"source": "agy_quota", "accounts": {DEFAULT_ACCOUNT: {"models": models}}}


def quota_snapshot() -> dict[str, dict[str, Any]]:
    """Every vendor's normalized shape that has a reading: ``{vendor: shape}``. Never raises."""
    out: dict[str, dict[str, Any]] = {}
    for vendor, read in _QUOTA_READERS.items():
        try:
            shape = read()
        except Exception:
            logger.debug("vendor_limits: %s quota unreadable", vendor, exc_info=True)
            shape = {}
        if shape:
            out[vendor] = shape
    return out


# ---------------------------------------------------------------------------
# Health: the one rule set applied to a reader's shape
# ---------------------------------------------------------------------------


def claude_health(now: float | None = None, rules: qr.QuotaRules | None = None) -> dict[str, Any]:
    """Claude's verdict: the binding window of the best account; ``out`` when every account is."""
    now = time.time() if now is None else now
    return qr.vendor_view(claude_quota(), rules, now=now)


def codex_health(
    reading: Mapping[str, Any] | None = None, rules: qr.QuotaRules | None = None
) -> dict[str, Any]:
    """Codex's verdict from the rollout reading. Its staleness is the rollout's own flag."""
    return qr.vendor_view(codex_quota(reading), rules, now=None)


def _iso_ts(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _finite(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def antigravity_health(
    models: list[str] | None = None,
    *,
    path: Path | None = None,
    now: float | None = None,
    rules: qr.QuotaRules | None = None,
) -> dict[str, Any]:
    """Antigravity's per-model verdict from the companion's file.

    ``per_model`` is each model's ``used_pct``. The vendor ``used_pct`` is the
    most-used of *models* (the offered candidates) when given, else of all; the
    vendor is ``out`` only when every one of those models is out and not yet
    reset, since its quotas are per model. Absent file -> ``{}`` (unknown).
    """
    now = time.time() if now is None else now
    return qr.vendor_view(antigravity_quota(path=path), rules, now=now, models=models)


def local_health(status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Which router roles (``subagent``, ``agent``) have an available lane now."""
    if status is None:
        status = _fleet_status()
    if not isinstance(status, Mapping):
        return {}
    roles = status.get("roles")
    lanes = status.get("lanes")
    if not isinstance(roles, Mapping) or not isinstance(lanes, Mapping):
        return {}
    up: dict[str, bool] = {}
    ctx: dict[str, int] = {}
    for role in ("subagent", "agent"):
        r = roles.get(role)
        lane = lanes.get(r.get("lane")) if isinstance(r, Mapping) else None
        up[role] = bool(isinstance(lane, Mapping) and lane.get("available"))
        # The window the role's model serves (``live_ctx`` per model on its lane):
        # what spawn.route reads ``estimate_tokens`` against.
        live = lane.get("live_ctx") if isinstance(lane, Mapping) else None
        model = r.get("model") if isinstance(r, Mapping) else None
        if isinstance(live, Mapping) and isinstance(live.get(model), int):
            ctx[role] = int(live[model])
    out: dict[str, Any] = {
        "out": not any(up.values()),
        "lanes": up,
        "source": "fleet_status",
        "as_of": time.time(),
    }
    if ctx:
        out["ctx"] = ctx
    # The router's host CPU reading (``host.cpu_busy``), when it publishes one.
    host = status.get("host")
    busy = host.get("cpu_busy") if isinstance(host, Mapping) else None
    if isinstance(busy, (bool, int, float)):
        out["cpu_busy"] = busy
    return out


def _fleet_status() -> Any:
    import urllib.request

    try:
        url = os.environ.get("KIROCREW_FLEET_STATUS_URL")
        if not url:
            from kiro_crew.platform.context import current_context

            getter = getattr(current_context().providers, "fleet_status_url", None)
            url = getter() if callable(getter) else None
        if not url:
            return None
        with urllib.request.urlopen(url, timeout=FLEET_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read(1 << 20))
    except Exception:
        logger.debug("vendor_limits: fleet status unreachable", exc_info=True)
        return None


#: Each vendor's normalized-shape reader (looked up at call time, so a test's patch holds).
_QUOTA_READERS: dict[str, Callable[[], dict[str, Any]]] = {
    "claude": lambda: claude_quota(),
    "codex": lambda: codex_quota(),
    "antigravity": lambda: antigravity_quota(),
}


# ---------------------------------------------------------------------------
# Quota-out marks: vendor + account + model group
# ---------------------------------------------------------------------------


def _model_group(vendor: str, model: str, account: str) -> tuple[str, list[str]]:
    """``(group key, every model in it)`` for *model*: quota is held per group, so one
    model out means its whole group is. A model the probe does not group stands alone."""
    try:
        shape = _QUOTA_READERS[vendor]() if vendor in _QUOTA_READERS else {}
        accounts = shape.get("accounts") or {}
        acct = accounts.get(account) or accounts.get(DEFAULT_ACCOUNT) or {}
        rows = acct.get("models") or {}
        group = (rows.get(model) or {}).get("group")
        if not isinstance(group, str) or not group:
            return model, [model]
        peers = [m for m, r in rows.items() if isinstance(r, dict) and r.get("group") == group]
        return group, sorted(set(peers) | {model})
    except Exception:
        return model, [model]


def _live_readings(vendor: str) -> dict[str, dict[str, Any]]:
    """Per account, the vendor's newest live reading as ``{as_of, out, room}``; ``{}`` when none.

    A per-model vendor reads its probe (``room`` = models not out, by the one rule
    set); every other vendor its health verdict (Claude: the rate-limit event every
    turn carries; Codex: the newest rollout reading). Read on every routing call, so
    a mark is reconciled against live data without waiting for a restart.
    """
    try:
        rule_set = qr.rules()
        out: dict[str, dict[str, Any]] = {}
        if vendor in PER_MODEL_VENDORS:
            shape = _QUOTA_READERS[vendor]() if vendor in _QUOTA_READERS else {}
            for acct, data in (shape.get("accounts") or {}).items():
                rows = (data or {}).get("models") or {}
                as_ofs = [_finite(r.get("as_of")) for r in rows.values()]
                as_of = max((a for a in as_ofs if a is not None), default=None)
                if not as_of:
                    continue
                room = {m for m, r in rows.items() if not rule_set.reading_out(r)}
                out[str(acct)] = {"as_of": as_of, "out": not room, "room": room}
            return out
        # Called bare, so a caller (or a test) that swaps a reader keeps its contract.
        if vendor == "claude":
            h = claude_health()
        elif vendor == "codex":
            h = codex_health()
        else:
            return {}
        views = h.get("accounts") if isinstance(h.get("accounts"), dict) else None
        for acct, view in (views or ({DEFAULT_ACCOUNT: h} if h else {})).items():
            if view and view.get("as_of"):
                out[str(acct)] = {
                    "as_of": float(view["as_of"]),
                    "out": bool(view.get("out")),
                    "room": None,
                }
        return out
    except Exception:
        return {}


def _stale_against_live(mark: Mapping[str, Any], model: str, live: Mapping[str, Any]) -> bool:
    """Whether a live reading newer than *mark* shows its account (or *model*) has quota.

    A mark is one refused turn's word, kept until the reset the error named; a newer
    live reading on the same account that is not out (an early reset, a plan
    change) outranks it.
    """
    as_of = float(live.get("as_of") or 0) if live else 0.0
    if not as_of or as_of <= float(mark.get("as_of") or 0) or live.get("out"):
        return False
    room = live.get("room")
    if room is None:
        return True
    return (model in room) if model else bool(room)


def _public(mark: Mapping[str, Any], account: str) -> dict[str, Any]:
    return {**{k: v for k, v in mark.items() if k != "models"}, "account": account}


def _covering(
    vendor: str, now: float
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]], set[str]]:
    """``(whole, per_model, accounts)``: per account, its live whole-account mark and its
    live per-model marks (a group mark expanded to each model it holds), after expiry
    and reconciliation; and every account the vendor is known to have."""
    with _lock:
        row = json.loads(json.dumps((_load().get(MARKS_KEY) or {}).get(vendor) or {}))
    live = _live_readings(vendor)
    whole: dict[str, dict[str, Any]] = {}
    per_model: dict[str, dict[str, dict[str, Any]]] = {}
    for acct, groups in row.items():
        if not isinstance(groups, dict):
            continue
        reading = live.get(acct) or {}
        for group, mark in groups.items():
            if not isinstance(mark, dict) or float(mark.get("until") or 0) <= now:
                continue
            if group == "":
                if not _stale_against_live(mark, "", reading):
                    whole[acct] = mark
                continue
            for m in mark.get("models") or [group]:
                if not _stale_against_live(mark, m, reading):
                    prev = per_model.setdefault(acct, {}).get(m)
                    if prev is None or float(prev["until"]) < float(mark["until"]):
                        per_model[acct][m] = {**mark, "model": m}
    marked = {a for a, g in row.items() if isinstance(g, dict) and g}
    return whole, per_model, marked | set(live)


def note_quota_out(
    vendor: str,
    *,
    until: float,
    model: str = "",
    detail: str = "",
    now: float | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """Mark *vendor*'s *account* out until *until* (epoch s) after a run hit its quota. Never raises.

    A vendor whose quota is held per model group (:data:`PER_MODEL_VENDORS`) with a
    *model* marks only that group; every other vendor marks the whole account.
    Returns the mark written (``{}`` when nothing could be). A later mark extends,
    never shortens, an earlier one.
    """
    try:
        now = float(time.time() if now is None else now)
        until = float(until)
        account = account or DEFAULT_ACCOUNT
        if not vendor or until <= now:
            return {}
        mark: dict[str, Any] = {
            "until": until,
            "as_of": now,
            "model": model or "",
            "detail": (detail or "")[:300],
        }
        group = ""
        if vendor in PER_MODEL_VENDORS and model:
            group, mark["models"] = _model_group(vendor, model, account)
        with _lock:
            store = _load()
            bucket = store.setdefault(MARKS_KEY, {}).setdefault(vendor, {})
            _merge_mark(bucket.setdefault(account, {}), group, mark)
            snapshot = json.loads(json.dumps(store))
        _persist(snapshot)
        return {**mark, "account": account, "group": group}
    except Exception:
        logger.debug("vendor_limits: quota out not recorded", exc_info=True)
        return {}


def quota_out(
    vendor: str, model: str = "", now: float | None = None, account: str | None = None
) -> dict[str, Any] | None:
    """The live quota mark covering *vendor* (and *model*), or ``None``. Never raises.

    With *account*, that account alone. Without one, the vendor is covered only when
    every account it is known to have is; the mark returned is the one that lifts
    first, since the vendor is usable again as soon as any account is.
    """
    try:
        now = time.time() if now is None else now
        whole, per_model, accounts = _covering(vendor, now)
        found: list[dict[str, Any]] = []
        for acct in [account] if account else sorted(accounts):
            mark = whole.get(acct)
            if mark is not None:
                found.append(_public(mark, acct))
                continue
            m = (per_model.get(acct) or {}).get(model) if model else None
            if m is None:
                return None
            found.append(_public(m, acct))
        if found:
            return min(found, key=lambda x: float(x["until"]))
    except Exception:
        logger.debug("vendor_limits: quota out unreadable", exc_info=True)
    return None


def quota_outs(now: float | None = None) -> dict[str, dict[str, Any]]:
    """Live quota marks: ``{vendor: {"until": s, "models": {id: until}}}``. Never raises.

    ``until`` is present when every account of the vendor is marked out whole (the
    earliest of their resets); ``models`` lists each model every account has out,
    at least one of them by a model-group mark (the earliest reset among them).
    """
    now = time.time() if now is None else now
    out: dict[str, dict[str, Any]] = {}
    try:
        with _lock:
            vendors = list((_load().get(MARKS_KEY) or {}).keys())
        for vendor in vendors:
            whole, per_model, accounts = _covering(vendor, now)
            if not accounts:
                continue
            state: dict[str, Any] = {}
            if all(a in whole for a in accounts):
                state["until"] = min(float(whole[a]["until"]) for a in accounts)
            models: dict[str, float] = {}
            for m in sorted({m for marks in per_model.values() for m in marks}):
                untils = []
                for a in accounts:
                    covers = [
                        float(x["until"])
                        for x in (whole.get(a), (per_model.get(a) or {}).get(m))
                        if x is not None
                    ]
                    if not covers:
                        break
                    untils.append(max(covers))
                else:
                    models[m] = min(untils)
            if models:
                state["models"] = models
            if state:
                out[vendor] = state
    except Exception:
        logger.debug("vendor_limits: quota outs unreadable", exc_info=True)
    return out


def is_low(state: Mapping[str, Any]) -> bool:
    """Whether *state* is at or past ``low_pct`` (:func:`kiro_crew.quota_rules.is_low`)."""
    return qr.is_low(state)


def _reset_for_tests() -> None:
    global _store
    with _lock:
        _store = None
