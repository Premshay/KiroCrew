"""Subscription headroom per vendor, from the signals each harness already emits.

``spawn.route`` (``decisions/points/spawn_route.py``) skips a vendor near or past
its window before a spawn fails on it (POLICY.md, operator ruling of 2026-11-20).
This module holds the readings it needs and turns each into one shape::

    {"out": bool, "used_pct": 0..100, "window": str, "resets_at": epoch s,
     "source": str, "as_of": epoch s}

``used_pct`` is the binding window's (the most-used one when a vendor has several).
An unknown value is an ABSENT key, never 0: a vendor nobody has read is usable,
not idle.

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
  Nexus companion: ``{as_of, models: {<id>: {remaining_fraction, reset_time}}}``.
- Local: ``GET /fleet/status`` of the fleet router (a role's lane is up).
- Quota outs: a run that HIT its vendor's quota marks the vendor (or, for
  Antigravity, the model's quota group) out until its reset at once
  (:func:`note_quota_out`, called by ``subagent_manager.quota_switch``), so no
  new spawn waits for a probe or a tombstone scan to learn it. Kept in
  ``vendor_limits.json`` under ``_outs``.

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
from typing import Any, Mapping

logger = logging.getLogger(__name__)

CLAUDE_RATE_LIMIT_META = "_claude/rateLimit"
STORE_NAME = "vendor_limits.json"
ANTIGRAVITY_QUOTA_NAME = "antigravity_quota.json"
#: The fleet router's status route; ``KIROCREW_FLEET_STATUS_URL`` overrides it.
FLEET_STATUS_URL = "http://127.0.0.1:8484/fleet/status"
FLEET_TIMEOUT_S = 1.5
#: A ``used_pct`` at or above this is "low" (half the window or less left).
LOW_PCT = 50.0

_lock = threading.Lock()
#: ``{vendor: {window: reading}}``; loaded from disk on first use.
_store: dict[str, dict[str, dict[str, Any]]] | None = None


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


def _load() -> dict[str, dict[str, dict[str, Any]]]:
    global _store
    if _store is not None:
        return _store
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    try:
        raw = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for vendor, windows in raw.items():
                if isinstance(vendor, str) and isinstance(windows, dict):
                    loaded[vendor] = {
                        str(w): dict(r) for w, r in windows.items() if isinstance(r, dict)
                    }
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


def note_claude_rate_limit(info: object, *, now: float | None = None) -> bool:
    """Keep one ``SDKRateLimitInfo``; ``True`` when it changed the store. Never raises."""
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
    with _lock:
        store = _load()
        prev = store.get("claude", {}).get(window)
        same = prev is not None and all(
            prev.get(k) == reading.get(k) for k in ("status", "used_pct", "resets_at")
        )
        store.setdefault("claude", {})[window] = reading
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


def claude_health(now: float | None = None) -> dict[str, Any]:
    """Claude's binding window: the most-used live one; ``out`` when any live window rejected."""
    now = time.time() if now is None else now
    with _lock:
        windows = {w: dict(r) for w, r in _load().get("claude", {}).items()}
    live = {
        w: r
        for w, r in windows.items()
        if r.get("resets_at") is None or float(r["resets_at"]) > now
    }
    if not live:
        return {}
    rejected = [w for w, r in live.items() if r.get("status") == "rejected"]
    if rejected:
        w = max(rejected, key=lambda k: float(live[k].get("resets_at") or 0))
    else:
        scored = [w for w, r in live.items() if "used_pct" in r]
        if not scored:
            w = max(live, key=lambda k: float(live[k].get("as_of") or 0))
        else:
            w = max(scored, key=lambda k: float(live[k]["used_pct"]))
    r = live[w]
    out: dict[str, Any] = {
        "out": bool(rejected),
        "window": w,
        "source": "claude_rate_limit_event",
        "as_of": max(float(x.get("as_of") or 0) for x in live.values()),
    }
    if rejected:
        out["used_pct"] = 100.0
    elif "used_pct" in r:
        out["used_pct"] = float(r["used_pct"])
    if r.get("resets_at") is not None:
        out["resets_at"] = r["resets_at"]
    return out


def codex_health(reading: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Codex's binding window from the rollout reading (``codex_usage``); ``out`` at 100 %."""
    if reading is None:
        try:
            from kiro_crew.dashboard.handlers.codex_usage import read_codex_rate_limits

            reading = read_codex_rate_limits()
        except Exception:
            logger.debug("vendor_limits: codex reading failed", exc_info=True)
            return {}
    if not reading:
        return {}
    best: tuple[str, Mapping[str, Any]] | None = None
    for name, label in (("five_hour", "five_hour"), ("weekly", "weekly")):
        w = reading.get(name)
        if not isinstance(w, Mapping) or w.get("stale"):
            continue
        used = _finite(w.get("used_percent"))
        if used is None:
            continue
        if best is None or used > float(best[1]["used_percent"]):
            best = (label, w)
    if best is None:
        return {}
    label, w = best
    used = float(w["used_percent"])
    out: dict[str, Any] = {
        "out": used >= 100.0,
        "used_pct": round(used, 1),
        "window": label,
        "source": "codex_rollout",
    }
    if w.get("resets_at") is not None:
        out["resets_at"] = w["resets_at"]
    as_of = _iso_ts(reading.get("captured_at")) or _finite(reading.get("source_mtime"))
    if as_of is not None:
        out["as_of"] = as_of
    return out


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
    models: list[str] | None = None, *, path: Path | None = None, now: float | None = None
) -> dict[str, Any]:
    """Antigravity's per-model quota from the companion's file.

    ``per_model`` is each model's ``used_pct``. The vendor ``used_pct`` is the
    most-used of *models* (the offered candidates) when given, else of all; the
    vendor is ``out`` only when every one of those models is used up and not yet
    reset, since its quotas are per model. Absent file -> ``{}`` (unknown).
    """
    now = time.time() if now is None else now
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
    per_model: dict[str, float] = {}
    resets: dict[str, float] = {}
    for mid, row in rows.items():
        if not isinstance(mid, str) or not isinstance(row, dict):
            continue
        rem = _finite(row.get("remaining_fraction"))
        if rem is None:
            continue
        reset = _iso_ts(row.get("reset_time"))
        if reset is not None and reset <= now:
            # The window rolled over since the probe: the reading no longer holds.
            continue
        per_model[mid] = round(100.0 * (1.0 - min(max(rem, 0.0), 1.0)), 1)
        if reset is not None:
            resets[mid] = reset
    if not per_model:
        return {}
    scope = [m for m in (models or []) if m in per_model] or list(per_model)
    worst = max(scope, key=lambda m: per_model[m])
    out: dict[str, Any] = {
        "out": all(per_model[m] >= 100.0 for m in scope),
        "used_pct": per_model[worst],
        "window": "per_model",
        "per_model": per_model,
        "source": "agy_quota",
    }
    if worst in resets:
        out["resets_at"] = resets[worst]
    as_of = _iso_ts(raw.get("as_of"))
    if as_of is not None:
        out["as_of"] = as_of
    return out


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

    url = os.environ.get("KIROCREW_FLEET_STATUS_URL") or FLEET_STATUS_URL
    try:
        with urllib.request.urlopen(url, timeout=FLEET_TIMEOUT_S) as resp:  # noqa: S310 - loopback
            return json.loads(resp.read(1 << 20))
    except Exception:
        logger.debug("vendor_limits: fleet status unreachable", exc_info=True)
        return None


OUTS_KEY = "_outs"


def _antigravity_group_peers(model: str, path: Path | None = None) -> list[str]:
    """Every model sharing *model*'s Antigravity quota group (the companion's probe file).

    Quota is held per group, so one model out means its whole group is. A model
    the file does not group stands alone.
    """
    try:
        raw = json.loads((path or _data_home() / ANTIGRAVITY_QUOTA_NAME).read_text("utf-8"))
        rows = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return [model]
        group = (rows.get(model) or {}).get("group") if isinstance(rows.get(model), dict) else None
        if not isinstance(group, str) or not group:
            return [model]
        peers = [m for m, r in rows.items() if isinstance(r, dict) and r.get("group") == group]
        return sorted(set(peers) | {model})
    except Exception:
        return [model]


def _antigravity_live_room(path: Path | None = None) -> tuple[float, set[str]]:
    """``(as_of, models with quota left)`` from the companion's live probe; ``(0, set())`` when unknown.

    A quota mark comes from one refused turn and holds until the reset the error
    named. The probe is read after every turn, so a newer probe that shows room
    on a model proves the mark is stale (an early reset, a plan change).
    """
    try:
        raw = json.loads((path or _data_home() / ANTIGRAVITY_QUOTA_NAME).read_text("utf-8"))
        as_of = _iso_ts(raw.get("as_of")) if isinstance(raw, dict) else None
        rows = raw.get("models") if isinstance(raw, dict) else None
        if as_of is None or not isinstance(rows, dict):
            return 0.0, set()
        room = {
            m
            for m, r in rows.items()
            if isinstance(r, dict) and (_finite(r.get("remaining_fraction")) or 0.0) > 0.0
        }
        return float(as_of), room
    except Exception:
        return 0.0, set()


def _stale_against_live(
    vendor: str, mark: Mapping[str, Any], model: str, live: tuple[float, set[str]]
) -> bool:
    """Whether a newer live probe shows room where *mark* claims none (Antigravity only)."""
    if vendor != "antigravity":
        return False
    as_of, room = live
    if not as_of or as_of <= float(mark.get("as_of") or 0):
        return False
    return (model in room) if model else bool(room)


def note_quota_out(
    vendor: str,
    *,
    until: float,
    model: str = "",
    detail: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Mark *vendor* out until *until* (epoch s) after a run hit its quota. Never raises.

    Antigravity's quota is per model group, so with a *model* only that group
    is marked; every other vendor is out whole. Returns the mark written (``{}``
    when nothing could be). A later mark extends, never shortens, an earlier one.
    """
    try:
        now = float(time.time() if now is None else now)
        until = float(until)
        if not vendor or until <= now:
            return {}
        mark: dict[str, Any] = {
            "until": until,
            "as_of": now,
            "model": model or "",
            "detail": (detail or "")[:300],
        }
        per_model = vendor == "antigravity" and bool(model)
        if per_model:
            mark["models"] = _antigravity_group_peers(model)
        with _lock:
            store = _load()
            outs = store.setdefault(OUTS_KEY, {})
            key = "models" if per_model else "vendor"
            row = outs.setdefault(vendor, {})
            if per_model:
                bucket = row.setdefault("models", {})
                for m in mark["models"]:
                    prev = bucket.get(m) or {}
                    if float(prev.get("until") or 0) < until:
                        bucket[m] = {k: v for k, v in mark.items() if k != "models"}
            else:
                prev = row.get(key) or {}
                if float(prev.get("until") or 0) < until:
                    row[key] = {k: v for k, v in mark.items() if k != "models"}
            snapshot = json.loads(json.dumps(store))
        _persist(snapshot)
        return mark
    except Exception:
        logger.debug("vendor_limits: quota out not recorded", exc_info=True)
        return {}


def quota_out(vendor: str, model: str = "", now: float | None = None) -> dict[str, Any] | None:
    """The live quota mark covering *vendor* (and *model*), or ``None``. Never raises."""
    try:
        now = time.time() if now is None else now
        with _lock:
            row = dict(_load().get(OUTS_KEY, {}).get(vendor) or {})
        live = _antigravity_live_room() if vendor == "antigravity" else (0.0, set())
        whole = row.get("vendor")
        if (
            isinstance(whole, dict)
            and float(whole.get("until") or 0) > now
            and not _stale_against_live(vendor, whole, "", live)
        ):
            return dict(whole)
        if model:
            m = (row.get("models") or {}).get(model)
            if (
                isinstance(m, dict)
                and float(m.get("until") or 0) > now
                and not _stale_against_live(vendor, m, model, live)
            ):
                return {**m, "model": model}
    except Exception:
        logger.debug("vendor_limits: quota out unreadable", exc_info=True)
    return None


def quota_outs(now: float | None = None) -> dict[str, dict[str, Any]]:
    """Live quota marks: ``{vendor: {"until": s, "models": {id: until}}}``; whole-vendor
    marks carry ``until``, per-model ones only ``models``. Never raises."""
    now = time.time() if now is None else now
    out: dict[str, dict[str, Any]] = {}
    try:
        with _lock:
            raw = json.loads(json.dumps(_load().get(OUTS_KEY, {})))
        for vendor, row in raw.items():
            if not isinstance(row, dict):
                continue
            state: dict[str, Any] = {}
            live = _antigravity_live_room() if vendor == "antigravity" else (0.0, set())
            whole = row.get("vendor")
            if (
                isinstance(whole, dict)
                and float(whole.get("until") or 0) > now
                and not _stale_against_live(vendor, whole, "", live)
            ):
                state["until"] = float(whole["until"])
            models = {
                m: float(r["until"])
                for m, r in (row.get("models") or {}).items()
                if isinstance(r, dict)
                and float(r.get("until") or 0) > now
                and not _stale_against_live(vendor, r, m, live)
            }
            if models:
                state["models"] = models
            if state:
                out[vendor] = state
    except Exception:
        logger.debug("vendor_limits: quota outs unreadable", exc_info=True)
    return out


def is_low(state: Mapping[str, Any]) -> bool:
    pct = state.get("used_pct")
    return isinstance(pct, (int, float)) and pct >= LOW_PCT


def _reset_for_tests() -> None:
    global _store
    with _lock:
        _store = None
