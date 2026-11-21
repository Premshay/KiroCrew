"""The one rule set for subscription quota: when a vendor is "low" and when it is "out".

Operator ruling of 2026-11-21: every vendor (Claude, Codex, Antigravity, DeepSeek,
local) may hold several accounts, and quota is judged the same way for each of
them, with the thresholds defined in ONE place. The defaults are
``decisions.quota_limits`` (:data:`kiro_crew.config.sections_decisions.QUOTA_LIMITS_DEFAULT`:
``low_pct`` 50, ``out_pct`` 95); this module turns them into verdicts.

The normalized shape
--------------------
Every vendor reader in :mod:`kiro_crew.vendor_limits` fills one shape and nothing
else::

    {"source": str,
     "accounts": {<account>: {"windows": {<window>: READING}}      # Claude, Codex
                            | {"models":  {<model id>: READING}}}}  # Antigravity

    READING = {"used_pct": 0..100, "resets_at": epoch s, "as_of": epoch s,
               "status": "allowed" | "allowed_warning" | "rejected", "group": str}

Every key of a reading is optional: an unknown value is an ABSENT key, never 0
(a vendor nobody has read is usable, not idle). ``group`` is the model's quota
group, for a vendor whose quota is held per model group. Until the harnesses say
which account served a turn, every reading belongs to :data:`DEFAULT_ACCOUNT`.

The rules
---------
- A reading is **out** when its status is ``rejected`` or its ``used_pct`` is at or
  above ``out_pct``; **low** when ``used_pct`` is at or above ``low_pct``.
- An account of windows is bound by its out window (a refused one first, then the
  one that resets last) when it has one, else by its most-used window; it is out
  when any window is.
- An account of models: its ``used_pct`` is the most-used model in scope, and it is
  out only when every model in scope is (quota is per model).
- A vendor is out only when EVERY account is out, and its ``used_pct`` is the best
  (least-used) account's, because sessions switch accounts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from kiro_crew.config.sections_decisions import (
    QUOTA_LOW_PCT_DEFAULT,
    QUOTA_OUT_PCT_DEFAULT,
    coerce_quota_limits,
)

#: The account every reading and mark belongs to until a harness names its account.
DEFAULT_ACCOUNT = "default"
#: The status a provider gives a refused request: out whatever the percentage says.
REJECTED = "rejected"


@dataclass(frozen=True)
class QuotaRules:
    """The two thresholds, as a percentage of a window used."""

    low_pct: float = QUOTA_LOW_PCT_DEFAULT
    out_pct: float = QUOTA_OUT_PCT_DEFAULT

    def reading_out(self, reading: Mapping[str, Any]) -> bool:
        if reading.get("status") == REJECTED:
            return True
        pct = _num(reading.get("used_pct"))
        return pct is not None and pct >= self.out_pct

    def pct_low(self, pct: object) -> bool:
        p = _num(pct)
        return p is not None and p >= self.low_pct

    def as_state(self) -> dict[str, float]:
        """The thresholds as the decision server reads them (``state["limits"]``)."""
        return {"low_pct": self.low_pct, "out_pct": self.out_pct}


DEFAULT_RULES = QuotaRules()


def rules(config: Any | None = None) -> QuotaRules:
    """The thresholds in force (``decisions.quota_limits``). Never raises; unreadable -> defaults."""
    try:
        from kiro_crew.decisions.gate import _decisions_config

        raw = getattr(_decisions_config(config), "quota_limits", None)
    except Exception:
        return DEFAULT_RULES
    limits = coerce_quota_limits(raw)
    return QuotaRules(low_pct=limits["low_pct"], out_pct=limits["out_pct"])


def is_low(state: Mapping[str, Any], rule_set: QuotaRules | None = None) -> bool:
    """Whether a vendor health *state* is at or past ``low_pct``."""
    return (rule_set or rules()).pct_low(state.get("used_pct"))


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _live(readings: Mapping[str, Mapping[str, Any]], now: float | None) -> dict[str, dict]:
    """*readings* minus those whose reset has passed (at *now*; ``None`` keeps all)."""
    out: dict[str, dict] = {}
    for key, r in readings.items():
        if not isinstance(r, Mapping):
            continue
        resets = _num(r.get("resets_at"))
        if now is not None and resets is not None and resets <= now:
            continue
        out[key] = dict(r)
    return out


def _windows_view(
    windows: Mapping[str, Mapping[str, Any]], rule_set: QuotaRules, now: float | None
) -> dict[str, Any]:
    live = _live(windows, now)
    if not live:
        return {}
    outs = [w for w, r in live.items() if rule_set.reading_out(r)]
    if outs:
        # A provider's refusal outranks a percentage: it is the reading a quota hit names.
        refused = [w for w in outs if live[w].get("status") == REJECTED]
        w = max(refused or outs, key=lambda k: float(_num(live[k].get("resets_at")) or 0))
    else:
        scored = [w for w, r in live.items() if _num(r.get("used_pct")) is not None]
        if scored:
            w = max(scored, key=lambda k: float(live[k]["used_pct"]))
        else:
            w = max(live, key=lambda k: float(_num(live[k].get("as_of")) or 0))
    r = live[w]
    view: dict[str, Any] = {"out": bool(outs), "window": w}
    if r.get("status") == REJECTED:
        view["used_pct"] = 100.0
    elif _num(r.get("used_pct")) is not None:
        view["used_pct"] = float(r["used_pct"])
    if r.get("resets_at") is not None:
        view["resets_at"] = r["resets_at"]
    if r.get("status"):
        view["status"] = r["status"]
    as_ofs = [_num(x.get("as_of")) for x in live.values()]
    as_ofs = [a for a in as_ofs if a is not None]
    if as_ofs:
        view["as_of"] = max(as_ofs)
    return view


def _models_view(
    models: Mapping[str, Mapping[str, Any]],
    rule_set: QuotaRules,
    now: float | None,
    scope: Sequence[str] | None,
) -> dict[str, Any]:
    live = {m: r for m, r in _live(models, now).items() if _num(r.get("used_pct")) is not None}
    if not live:
        return {}
    per_model = {m: float(r["used_pct"]) for m, r in live.items()}
    in_scope = [m for m in (scope or []) if m in live] or list(live)
    worst = max(in_scope, key=lambda m: per_model[m])
    view: dict[str, Any] = {
        "out": all(rule_set.reading_out(live[m]) for m in in_scope),
        "used_pct": per_model[worst],
        "window": "per_model",
        "per_model": per_model,
    }
    if live[worst].get("resets_at") is not None:
        view["resets_at"] = live[worst]["resets_at"]
    as_ofs = [a for a in (_num(r.get("as_of")) for r in live.values()) if a is not None]
    if as_ofs:
        view["as_of"] = max(as_ofs)
    return view


def account_view(
    account: Mapping[str, Any],
    rule_set: QuotaRules | None = None,
    *,
    now: float | None = None,
    models: Sequence[str] | None = None,
) -> dict[str, Any]:
    """One account's verdict ``{out, used_pct?, window, resets_at?, as_of?, ...}``; ``{}`` = unknown.

    Readings whose reset passed before *now* no longer hold; ``now=None`` keeps
    every reading (a source that judges its own staleness, like Codex's rollout).
    *models* scopes a per-model account to the offered candidates.
    """
    rule_set = rule_set or rules()
    if isinstance(account.get("models"), Mapping):
        return _models_view(account["models"], rule_set, now, models)
    if isinstance(account.get("windows"), Mapping):
        return _windows_view(account["windows"], rule_set, now)
    return {}


def aggregate_accounts(views: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The vendor's verdict from its accounts' verdicts (unknown accounts skipped).

    Out only when every known account is out. Otherwise the vendor reads as its
    best usable account: the least-used one (an account with no percentage ranks
    after every scored one). When all are out, the account that resets first binds.
    """
    known = {a: dict(v) for a, v in views.items() if v}
    if not known:
        return {}
    usable = {a: v for a, v in known.items() if not v.get("out")}
    if usable:

        def rank(a: str) -> tuple[int, float]:
            pct = _num(usable[a].get("used_pct"))
            return (0, pct) if pct is not None else (1, 0.0)

        best = min(usable, key=rank)
    else:
        best = min(known, key=lambda a: _num(known[a].get("resets_at")) or math.inf)
    out = dict(known[best])
    out["out"] = not usable
    per_models = [v["per_model"] for v in (usable or known).values() if v.get("per_model")]
    if len(per_models) > 1:
        merged: dict[str, float] = {}
        for pm in per_models:
            for m, p in pm.items():
                merged[m] = min(p, merged.get(m, p))
        out["per_model"] = merged
    as_ofs = [a for a in (_num(v.get("as_of")) for v in known.values()) if a is not None]
    if as_ofs:
        out["as_of"] = max(as_ofs)
    # Named only once there is more than the one default account, so today's
    # single-account verdict keeps its exact shape.
    if set(known) != {DEFAULT_ACCOUNT}:
        out["account"] = best
        out["accounts"] = known
    return out


def vendor_view(
    shape: Mapping[str, Any],
    rule_set: QuotaRules | None = None,
    *,
    now: float | None = None,
    models: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A vendor's verdict from its normalized *shape*: each account judged, then aggregated."""
    rule_set = rule_set or rules()
    accounts = shape.get("accounts") if isinstance(shape, Mapping) else None
    if not isinstance(accounts, Mapping):
        return {}
    views = {
        str(a): account_view(acc, rule_set, now=now, models=models)
        for a, acc in accounts.items()
        if isinstance(acc, Mapping)
    }
    out = aggregate_accounts(views)
    if out and shape.get("source"):
        out["source"] = shape["source"]
    return out
