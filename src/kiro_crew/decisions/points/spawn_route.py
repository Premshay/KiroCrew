"""``spawn.route`` -- which model tier should run a sub-agent brief?

A sub-agent runs on the per-spawn ``model`` pin, else ``agent.role_models['subagent']``,
else the seat's own default: one model for every brief the seat is handed, whether
it is a read-only inventory or a cross-cutting feature. This point asks the oracle,
for a spawn that names NO model, which of the owner's configured tiers for the
child's vendor should run it, and records why on the child's card.

The unit is the model TIER within the child's vendor: the tiers are derived from
the models the vendor's adapter ADVERTISES, sorted by the operator's family rule
(:data:`FAMILY_TIERS` -- top fable/astra/pro, large opus/sol/pro, small
sonnet/terra/flash, mini haiku/luna/flash), and ``decisions.spawn_route`` may override a
tier with explicit ids; the seat -- crew, memory silo, project
context -- stays what the parent chose. A vendor the tombstones show rate-limited
is reported in the state so the oracle can say so in its reason; switching the
seat to another vendor is a follow-up, because a seat carries a memory silo and
this point must not move one behind the parent's back.

Never behind the caller's back
------------------------------
A per-spawn ``model`` is the parent's answer to this question and is never
overridden. No model id is hardcoded (``model-selection.md``): a candidate is
always an id the adapter advertised to this account, or one the owner pinned. The
point runs only with the Decisions keystone consented, like every other point.

Everything is a refusal back to the seat's own model
----------------------------------------------------
:func:`routed_spawn` returns ``None`` for: the seam is off, the session is not
sampled, no candidate is configured for the child's vendor, the answer is outside
the offered candidates, the transport failed, or the budget expired.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from kiro_crew import decisions as core
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "spawn.route"
QUESTION_ID = "spawn"

#: The vendors a seat name can carry and the tiers the owner may pin per vendor.
VENDORS: tuple[str, ...] = ("claude", "codex", "deepseek", "local", "antigravity")
#: Rungs, hardest first: ``top`` is the frontier supervisor (orchestration, long-horizon
#: builds), ``mini`` the cheapest bounded worker.
TIERS: tuple[str, ...] = ("top", "large", "small", "mini")

#: Characters of the brief sent with the question -- the bound ``skills.select``
#: and ``model.route`` apply to a message, and a brief is a message to the child.
MAX_BRIEF_CHARS = 2000

#: How long a vendor is read as out after a limit hit whose error names no reset
#: (Claude's ``rate_limit`` carries none): the 5-hour rolling window.
DEFAULT_WINDOW_S = 5 * 3600
#: Bedrock throttling is a blip, not a quota.
THROTTLE_WINDOW_S = 120
#: A named reset a week out is an upper bound, not the truth: the tombstones show
#: deliveries resuming 9-36 h after each one. After this long the caller may send
#: one spawn to find out; a delivery clears the outage whatever the reset said.
PROBE_AFTER_S = 5 * 3600
#: How long a tombstone scan is reused before the directory is read again.
HEALTH_CACHE_S = 60

_LIMIT_MARKS = (
    (re.compile(r"usageLimitExceeded|hit your usage limit", re.I), DEFAULT_WINDOW_S),
    (re.compile(r"errorKind':\s*'rate_limit'|rate[_ ]limit", re.I), DEFAULT_WINDOW_S),
    (re.compile(r"throttling requests", re.I), THROTTLE_WINDOW_S),
)
# "try again at 4:46 PM."  /  "try again at Sep 24th, 2026 12:03 AM."  (local wall clock)
_AT_CLOCK = re.compile(r"try again at (\d{1,2}):(\d{2}) ?(AM|PM)", re.I)
_AT_DATE = re.compile(
    r"try again at ([A-Z][a-z]{2}) (\d{1,2})(?:st|nd|rd|th)?, (\d{4}) (\d{1,2}):(\d{2}) ?(AM|PM)",
    re.I,
)
_MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
_MODEL = re.compile(r"usage limit for ([A-Za-z0-9.\-]+?)\.?(?:\s|$)")

_health_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def vendor_of_seat(agent: str, model: str = "") -> str:
    """The vendor a seat name (``crew-codex-atlas``) or, failing that, a model id names; ``""`` unknown."""
    a = (agent or "").lower()
    for v in ("codex", "deepseek", "antigravity", "local", "claude"):
        if v in a:
            return v
    m = (model or "").lower()
    if m.startswith(("gpt-", "o1", "o3", "o4", "codex")):
        return "codex"
    if m.startswith("claude"):
        return "claude"
    if m.startswith("deepseek"):
        return "deepseek"
    return ""


def _vendor_of_tombstone(row: Mapping[str, Any]) -> str:
    p = str(row.get("provider") or "").lower()
    a = str(row.get("agent") or "").lower()
    if p.startswith("codex") or "codex" in a:
        return "codex"
    if p == "deepseek" or "deepseek" in a:
        return "deepseek"
    if "antigravity" in a:
        return "antigravity"
    if "local" in a:
        return "local"
    if p in ("claude_code", "acp") or "claude" in a or a in ("kirocrew", ""):
        return "claude"
    return ""


def _to_24h(h: int, m: int, ampm: str) -> tuple[int, int]:
    return h % 12 + (12 if ampm.upper() == "PM" else 0), m


def reset_time(detail: str, died: float, window_s: int) -> float:
    """When the error says the vendor is back: a named date, a named clock (today, else tomorrow), else died+window."""
    m = _AT_DATE.search(detail)
    if m:
        mon, day, year, h, mi, ampm = m.groups()
        h, mi = _to_24h(int(h), int(mi), ampm)
        return (
            _dt.datetime(int(year), _MONTHS[mon.title()], int(day), h, mi).astimezone().timestamp()
        )
    m = _AT_CLOCK.search(detail)
    if m:
        h, mi = _to_24h(int(m.group(1)), int(m.group(2)), m.group(3))
        t = (
            _dt.datetime.fromtimestamp(died)
            .astimezone()
            .replace(hour=h, minute=mi, second=0, microsecond=0)
        )
        if t.timestamp() < died - 60:
            t += _dt.timedelta(days=1)
        return t.timestamp()
    return died + window_s


def limit_hit(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The outage a tombstone proves, or ``None`` when it is not a quota or rate-limit death."""
    detail = str(row.get("detail") or "")
    if not detail:
        return None
    for rx, window in _LIMIT_MARKS:
        if rx.search(detail):
            died = float(row.get("died") or 0)
            mm = _MODEL.search(detail)
            return {
                "vendor": _vendor_of_tombstone(row),
                "from": died,
                "until": reset_time(detail, died, window),
                "model": mm.group(1) if mm else "",
            }
    return None


def vendor_health(
    rows: list[Mapping[str, Any]], now: float | None = None
) -> dict[str, dict[str, Any]]:
    """Per vendor: ``out`` when its LAST event was a limit hit and the reset has not passed.

    A delivery after the hit clears it whatever the named reset said. ``probe_after``
    is when one spawn may be sent to find out; ``until`` is the named or default reset.
    """
    now = time.time() if now is None else now
    out: dict[str, dict[str, Any]] = {
        v: {
            "out": False,
            "until": None,
            "probe_after": None,
            "models": [],
            "last_hit": None,
            "last_delivery": None,
        }
        for v in VENDORS
    }
    for r in rows:
        v = _vendor_of_tombstone(r)
        if not v:
            continue
        s = out.setdefault(v, dict(out["claude"]))
        t = float(r.get("died") or r.get("started") or 0)
        if t > now:
            continue
        h = limit_hit(r)
        if h:
            if (s["last_hit"] or 0) < h["from"]:
                s["last_hit"] = h["from"]
                s["until"] = h["until"]
                s["models"] = [h["model"]] if h["model"] else []
        elif (
            r.get("cause") in ("delivered", "result_available")
            or r.get("recovery_action") == "delivered"
        ):
            s["last_delivery"] = max(s["last_delivery"] or 0, t)
    for s in out.values():
        hit = s["last_hit"]
        if hit and (s["last_delivery"] or 0) < hit and now < (s["until"] or 0):
            s["out"] = True
            s["probe_after"] = min(s["until"], hit + PROBE_AFTER_S)
        else:
            s["until"] = None
            s["models"] = []
    return out


def load_tombstones(root: Path | None = None) -> list[dict[str, Any]]:
    """Every readable ``tombstone.json`` under the subagents registry. Never raises."""
    if root is None:
        from kiro_crew.subagent_persistence import _subagents_dir

        root = _subagents_dir()
    rows: list[dict[str, Any]] = []
    try:
        for f in Path(root).glob("*/tombstone.json"):
            try:
                rows.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
    except Exception:
        logger.debug("spawn.route: tombstones unreadable", exc_info=True)
    return rows


def cached_health(now: float | None = None) -> dict[str, dict[str, Any]]:
    """:func:`vendor_health` over the registry, reused for :data:`HEALTH_CACHE_S`."""
    global _health_cache
    now = time.time() if now is None else now
    if _health_cache is not None and now - _health_cache[0] < HEALTH_CACHE_S:
        return _health_cache[1]
    health = vendor_health(load_tombstones(), now)
    _health_cache = (now, health)
    return health


def tier_map(config: Any | None = None) -> dict[str, dict[str, list[str]]]:
    """``decisions.spawn_route`` as ``{vendor: {tier: [model ids]}}``. Never raises; unreadable reads as empty."""
    try:
        from kiro_crew.decisions.gate import _decisions_config

        raw = getattr(_decisions_config(config), "spawn_route", None)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for vendor, tiers in raw.items():
        if vendor not in VENDORS or not isinstance(tiers, dict):
            continue
        out[vendor] = {}
        for tier in TIERS:
            ids = tiers.get(tier)
            if isinstance(ids, str):
                ids = [ids]
            out[vendor][tier] = [
                str(i).strip() for i in (ids or []) if isinstance(i, str) and str(i).strip()
            ]
    return out


#: The provider namespace each vendor's adapter advertises its models under
#: (``model_registry.advertised_models``), i.e. ``provider_models.json``.
ADVERTISED_NAMESPACE: dict[str, str] = {
    "claude": "claude_code",
    "codex": "codex",
    "deepseek": "deepseek",
}

#: Which model FAMILIES are each tier, per vendor -- the operator's taxonomy
#: (POLICY.md "Spawn routing policy"), not model ids: the ids come from what the
#: adapter advertises, so a new version of a family is picked up with no edit.
#: Order within a tier is preference: the cheaper frontier model first.
FAMILY_TIERS: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {"top": ("fable",), "large": ("opus",), "small": ("sonnet",), "mini": ("haiku",)},
    "codex": {"top": ("astra",), "large": ("sol",), "small": ("terra",), "mini": ("luna",)},
    "deepseek": {"top": ("pro",), "large": ("pro",), "small": ("flash",), "mini": ("flash",)},
}

#: Codex advertises one id per reasoning effort (``gpt-5.6-sol[high]``); the
#: effort a tier runs at when the adapter offers no bare id.
TIER_EFFORT = {"top": "high", "large": "high", "small": "medium", "mini": "medium"}

_EFFORT_SUFFIX = re.compile(r"\[(low|medium|high|xhigh|max|ultra)\]$")
_EXCLUDE = re.compile(r"vision|exp\b|-exp|preview", re.I)


def _family_rank(model_id: str, families: tuple[str, ...]) -> int | None:
    low = model_id.lower()
    for i, fam in enumerate(families):
        if re.search(rf"(^|[^a-z]){re.escape(fam)}([^a-z]|$)", low):
            return i
    return None


def advertised_tiers(vendor: str, advertised: list[str] | None = None) -> dict[str, list[str]]:
    """``{large: [ids], small: [ids]}`` for *vendor* from its adapter's advertised list.

    An id is in a tier when its name carries one of the tier's families; ids that
    name none (``default``, ``agent``, an older ``gpt-5.5``) and experimental
    variants are left out. Per model, one id: the bare id when advertised, else the
    tier's effort variant. Never raises; a cold cache reads as empty.
    """
    families = FAMILY_TIERS.get(vendor)
    if not families:
        return {}
    if advertised is None:
        try:
            from kiro_crew import model_registry

            advertised = model_registry.advertised_models(ADVERTISED_NAMESPACE[vendor])
        except Exception:
            logger.debug("spawn.route: advertised models unreadable", exc_info=True)
            return {}
    out: dict[str, list[str]] = {}
    for tier, fams in families.items():
        by_model: dict[str, list[str]] = {}
        ranks: dict[str, int] = {}
        for mid in advertised or []:
            if not isinstance(mid, str) or _EXCLUDE.search(mid):
                continue
            rank = _family_rank(mid, fams)
            if rank is None:
                continue
            base = _EFFORT_SUFFIX.sub("", mid)
            by_model.setdefault(base, []).append(mid)
            ranks[base] = min(ranks.get(base, rank), rank)
        picked = []
        for base in sorted(by_model, key=lambda b: ranks[b]):
            ids = by_model[base]
            if base in ids:
                picked.append(base)
            else:
                want = f"{base}[{TIER_EFFORT[tier]}]"
                picked.append(want if want in ids else ids[0])
        out[tier] = picked
    return out


def effective_tiers(vendor: str, config: Any | None = None) -> dict[str, list[str]]:
    """The owner's ``decisions.spawn_route`` pins for *vendor* where set, else the advertised tiers.

    Per TIER: a tier the owner pinned uses the pins, an unpinned tier falls back to
    what the adapter advertises, so a partial override does not blank the rest.
    """
    pinned = tier_map(config).get(vendor, {})
    derived = advertised_tiers(vendor)
    return {tier: list(pinned.get(tier) or derived.get(tier) or []) for tier in TIERS}


def candidates_for(
    vendor: str, mapping: Mapping[str, Mapping[str, list[str]]]
) -> list[dict[str, str]]:
    """The offered options: one per pinned model id of *vendor*, keyed ``vendor/tier/i``."""
    out = []
    for tier in TIERS:
        for i, model in enumerate(mapping.get(vendor, {}).get(tier, [])):
            out.append(
                {"key": f"{vendor}/{tier}/{i}", "vendor": vendor, "tier": tier, "model": model}
            )
    return out


def questions(cands: list[dict[str, str]]) -> list[Question]:
    return [
        Choice(
            QUESTION_ID,
            "A parent agent is delegating this brief to a sub-agent. Which of the configured "
            "model rungs should run it? top = a supervisor that orchestrates other agents or "
            "long-horizon cross-cutting work; large = complex work; small = ordinary bounded work; "
            "mini = simple read-mostly work. Prefer a different model from the parent's for a review.",
            options=[c["key"] for c in cands],
        )
    ]


def build_state(
    brief: str,
    *,
    agent: str = "",
    vendor: str,
    parent_model: str,
    cands: list[dict[str, str]],
    health: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "brief": (brief or "")[:MAX_BRIEF_CHARS],
        # the child's seat: a conductor/orchestrator agent is itself the orchestration cue
        "agent": agent or "",
        "parent_vendor": vendor,
        "parent_model": parent_model,
        "candidates": [dict(c) for c in cands],
        "health": {
            v: {
                "out": bool(s.get("out")),
                "until": s.get("until"),
                "probe_after": s.get("probe_after"),
            }
            for v, s in health.items()
            if s.get("out")
        },
    }


def read_choice(
    answers: Any, cands: list[dict[str, str]]
) -> tuple[dict[str, str] | None, float | None, Any]:
    """(candidate, p, provider note) for the answered key, or ``(None, None, None)``."""
    if not isinstance(answers, dict):
        return None, None, None
    answer = answers.get(QUESTION_ID)
    if not isinstance(answer, Answer):
        return None, None, None
    for c in cands:
        if c["key"] == answer.value:
            return c, answer.p, answer.note
    return None, None, None


async def routed_spawn(
    brief: str,
    *,
    agent: str,
    parent_model: str = "",
    session_key: str | None = None,
    config: Any | None = None,
) -> dict[str, Any] | None:
    """The model a model-less spawn should run on, or ``None`` to leave the seat's default.

    Returns ``{route_id, model, tier, vendor, p, reason, vendor_out, latency_ms}``;
    ``reason`` is the provider's own one-line account (role, complexity, tier) when
    it sent one, so the child's card can show why. Never raises except
    :class:`asyncio.CancelledError`.
    """
    started = time.monotonic()
    route_id = uuid.uuid4().hex[:16]
    try:
        vendor = vendor_of_seat(agent, parent_model)
        if not vendor:
            return None
        tiers = await asyncio.to_thread(effective_tiers, vendor, config)
        cands = candidates_for(vendor, {vendor: tiers})
        if not cands:
            return None
        health = await asyncio.to_thread(cached_health)
        state = build_state(
            brief, agent=agent, vendor=vendor, parent_model=parent_model, cands=cands, health=health
        )
        answers = await core.decide(
            POINT,
            state,
            questions(cands),
            session_key=session_key,
            config=config,
            extra={
                "route_id": route_id,
                "vendor": vendor,
                "vendor_out": bool(health.get(vendor, {}).get("out")),
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("spawn.route: keeping the seat's own model", exc_info=True)
        return None
    chosen, p, reason = read_choice(answers, cands)
    if chosen is None:
        return None
    vendor_state = health.get(chosen["vendor"], {})
    return {
        "route_id": route_id,
        "model": chosen["model"],
        "tier": chosen["tier"],
        "vendor": chosen["vendor"],
        "p": p,
        "reason": reason if isinstance(reason, str) else "",
        "vendor_out": bool(vendor_state.get("out")),
        "vendor_until": vendor_state.get("until"),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def reason_line(routed: Mapping[str, Any]) -> str:
    """One line for the child's card: ``spawn.route: build/complex -> codex large (gpt-5.6-sol) p=0.90``."""
    r = routed.get("reason")
    head = f"{r[:80]} -> " if isinstance(r, str) and r else ""
    p = routed.get("p")
    line = f"spawn.route: {head}{routed.get('vendor')} {routed.get('tier')} ({routed.get('model')})"
    if isinstance(p, (int, float)):
        line += f" p={p:.2f}"
    if routed.get("vendor_out"):
        until = routed.get("vendor_until")
        when = _dt.datetime.fromtimestamp(until).astimezone().strftime("%H:%M") if until else "?"
        line += f"; vendor rate-limited until {when}"
    return line
