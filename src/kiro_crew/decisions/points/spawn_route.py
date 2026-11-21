"""``spawn.route`` -- which model tier should run a sub-agent brief?

A sub-agent runs on the per-spawn ``model`` pin, else ``agent.role_models['subagent']``,
else the seat's own default: one model for every brief the seat is handed, whether
it is a read-only inventory or a cross-cutting feature. This point asks the oracle,
for a spawn that names NO model, which of the owner's configured tiers for the
child's vendor should run it, and records why on the child's card.

The unit is a (vendor, model tier) candidate: the tiers are derived from the
models each vendor's adapter ADVERTISES, sorted by the operator's family rule
(:data:`FAMILY_TIERS` -- top fable/astra/pro, large opus/sol/pro/neon, small
sonnet/terra/flash/hydrogen/neon, mini haiku/luna/flash), and
``decisions.spawn_route`` may override a tier with explicit ids.

Since the operator ruling of 2026-11-20 (nexus ``eval/decision-lane/POLICY.md``)
the candidates span every vendor this host can run a child on, not only the
parent's: a candidate of another vendor carries the ``backend`` that runs it --
a companion execution seat (``execution_seats.declared_execution_seats``) or a
native ACP backend -- and the child keeps the parent's crew and memory identity,
because an execution seat changes the transport, not the memory owner (fork
PR #79). The local fleet is offered as its router roles ``subagent`` (CPU) and
``agent`` (GPU). DeepSeek seats are metered, so DeepSeek is offered only to a
DeepSeek parent, never as a silent switch. Each vendor's headroom
(:mod:`kiro_crew.vendor_limits`) rides in the state so the oracle can skip a
vendor that is out or low.

Never behind the caller's back
------------------------------
A per-spawn ``model`` is the parent's answer to this question and is never
overridden; a per-spawn ``backend``, execution seat, crew or named agent pins
the vendor, so the route stays within it (``cross_vendor=False``). No model id is hardcoded (``model-selection.md``): a candidate is
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
from typing import Any, Mapping, Sequence

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
# Antigravity: "Individual quota reached. ... Resets in 1h30m18s."
_RESETS_IN = re.compile(r"Resets? in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", re.I)
_MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
_MODEL = re.compile(r"usage limit for ([A-Za-z0-9.\-]+?)\.?(?:\s|$)")

_health_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _vendor_of_name(agent: str, model: str = "") -> str:
    """The vendor a seat or backend name (``crew-codex-atlas``) or, failing that, a model id names."""
    a = (agent or "").lower()
    for v in ("codex", "deepseek", "antigravity", "local", "claude"):
        if v in a:
            return v
    m = (model or "").lower()
    if m.startswith(("gpt-", "o1", "o3", "o4", "codex")):
        return "codex"
    if m.startswith(("claude", "fable", "opus", "sonnet", "haiku")):
        return "claude"
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("gemini"):
        return "antigravity"
    return ""


def vendor_of_seat(agent: str, model: str = "") -> str:
    """The vendor a seat name (``crew-codex-atlas``) or, failing that, a model id names; ``""`` unknown."""
    return _vendor_of_name(agent, model) or _vendor_of_runtime_policy(agent)


def served_vendor(
    *,
    runtime_agent: str = "",
    provider: str = "",
    model: str = "",
    agent: str = "",
    policy: bool = True,
) -> str:
    """The vendor that actually served a run: execution seat, then provider, then model, then seat.

    The template seat (``agent``) is the weakest witness: 2026-11-20 run
    521d4974 ran the ``crew-claude`` template on the ``crew-antigravity`` seat
    (``runtime_agent``) and was read as Claude. ``provider`` counts only when it
    names a vendor (``acp`` does not). *policy* lets a seat that names no vendor
    fall to the engine map; a bulk scan turns it off (one config load per row).
    """
    if runtime_agent:
        v = _vendor_of_name(runtime_agent) or (
            _vendor_of_runtime_policy(runtime_agent) if policy else ""
        )
        if v:
            return v
    v = _vendor_of_name(provider) or _vendor_of_name("", model) or _vendor_of_name(agent)
    if v:
        return v
    return _vendor_of_runtime_policy(agent) if policy else ""


def _vendor_of_runtime_policy(agent: str) -> str:
    """The vendor the companion's engine map runs *agent* on (``agent_runtime_policy``); ``""`` unknown.

    A crewmate seat (``derrick``) names no vendor and a cold parent reports no
    model, so the operator's engine row is the remaining witness.
    """
    if not agent:
        return ""
    try:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.context import current_context

        lookup = getattr(current_context().providers, "agent_runtime_policy", None)
        if not callable(lookup):
            return ""
        binding = KiroCrewConfig.load().agents.get(agent)
        identity = getattr(binding, "kiro_agent", "") or agent
        policy = lookup(identity)
    except Exception:
        return ""
    if not isinstance(policy, dict):
        return ""
    if str(policy.get("engine") or "").startswith("local"):
        return "local"
    backend = str(policy.get("backend") or "")
    if backend.startswith("antigravity"):
        return "antigravity"
    return backend if backend in ("claude", "codex", "deepseek") else ""


def _vendor_of_tombstone(row: Mapping[str, Any]) -> str:
    v = served_vendor(
        runtime_agent=str(row.get("runtime_agent") or ""),
        provider=str(row.get("provider") or ""),
        model=str(row.get("resolved_model") or row.get("model") or ""),
        agent=str(row.get("agent") or ""),
        policy=False,
    )
    if v:
        return v
    p = str(row.get("provider") or "").lower()
    a = str(row.get("agent") or "").lower()
    # A base seat on the shared ACP adapter or Claude Code names no vendor: Claude.
    if p in ("claude_code", "acp") or a in ("kirocrew", ""):
        return "claude"
    return ""


def _to_24h(h: int, m: int, ampm: str) -> tuple[int, int]:
    return h % 12 + (12 if ampm.upper() == "PM" else 0), m


def resets_in(detail: str, base: float) -> float | None:
    """*base* plus a relative ``Resets in 2h23m58s``, or ``None`` when *detail* names none."""
    m = _RESETS_IN.search(detail)
    if not (m and any(m.groups())):
        return None
    h, mi, se = (int(x or 0) for x in m.groups())
    return base + h * 3600 + mi * 60 + se


def reset_time(detail: str, died: float, window_s: int) -> float:
    """When the error says the vendor is back: a named date, a named clock (today, else
    tomorrow), a relative ``Resets in``, else died+window."""
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
    rel = resets_in(detail, died)
    if rel is not None:
        return rel
    return died + window_s


def limit_hit(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The outage a tombstone proves, or ``None`` when it is not a quota or rate-limit death.

    The generic marks read ``detail``. The vendor's own quota signals
    (:data:`quota_switch.QUOTA_PATTERNS`, e.g. Antigravity's ``Individual quota
    reached``) also read ``result_tail``: a refused Antigravity turn's detail is
    the generic ``failed (refusal)`` and the fault is the last line it streamed.
    """
    from kiro_crew.subagent_manager.quota_switch import QUOTA_PATTERNS

    detail = str(row.get("detail") or "")
    tail = str(row.get("result_tail") or "")
    if not detail and not tail:
        return None
    vendor = _vendor_of_tombstone(row)
    died = float(row.get("died") or 0)
    window = 0
    for rx, w in _LIMIT_MARKS:
        if detail and rx.search(detail):
            window = w
            break
    else:
        text = f"{detail}\n{tail}"
        if any(rx.search(text) for rx in QUOTA_PATTERNS.get(vendor, ())):
            window = DEFAULT_WINDOW_S
            detail = text
    if not window:
        return None
    mm = _MODEL.search(detail)
    return {
        "vendor": vendor,
        "from": died,
        "until": reset_time(detail, died, window),
        "model": mm.group(1) if mm else str(row.get("resolved_model") or ""),
    }


def vendor_health(
    rows: Sequence[Mapping[str, Any]], now: float | None = None
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


#: How much of a failed run's ``result.txt`` the scan reads for its vendor's fault
#: line (``quota_switch.RESULT_TAIL_CHARS``: the fault is the last thing it streamed).
_RESULT_TAIL_BYTES = 600


def _enrich_tombstone(row: dict[str, Any], folder: Path) -> None:
    """Add what a failed run's tombstone lacks from its own folder. Never raises.

    The serving seat and model live in ``state.json`` (``runtime_agent``,
    ``resolved_model``) and the provider's fault in ``result.txt``'s tail, read
    from the folder itself, never the ``result_path`` the file names.
    """
    if row.get("outcome") != "failed":
        return
    if not row.get("runtime_agent") or not row.get("resolved_model"):
        try:
            state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        except Exception:
            state = None
        if isinstance(state, dict):
            for key in ("runtime_agent", "resolved_model"):
                if not row.get(key) and isinstance(state.get(key), str) and state[key]:
                    row[key] = state[key]
    if row.get("result_available") and "result_tail" not in row:
        try:
            with open(folder / "result.txt", "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - _RESULT_TAIL_BYTES))
                row["result_tail"] = fh.read().decode("utf-8", "replace")
        except Exception:
            pass


def load_tombstones(root: Path | None = None) -> list[dict[str, Any]]:
    """Every readable ``tombstone.json`` under the subagents registry. Never raises.

    A failed run's row also carries its serving seat, model and result tail
    (:func:`_enrich_tombstone`), which the tombstone itself does not record.
    """
    if root is None:
        from kiro_crew.subagent_persistence import _subagents_dir

        root = _subagents_dir()
    rows: list[dict[str, Any]] = []
    try:
        for f in Path(root).glob("*/tombstone.json"):
            try:
                row = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(row, dict):
                _enrich_tombstone(row, f.parent)
                rows.append(row)
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
    "antigravity": "antigravity",
}

#: Which model FAMILIES are each tier, per vendor -- the operator's taxonomy
#: (POLICY.md "Spawn routing policy"), not model ids: the ids come from what the
#: adapter advertises, so a new version of a family is picked up with no edit.
#: Order within a tier is preference: the cheaper frontier model first.
FAMILY_TIERS: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {"top": ("fable",), "large": ("opus",), "small": ("sonnet",), "mini": ("haiku",)},
    "codex": {"top": ("astra",), "large": ("sol",), "small": ("terra",), "mini": ("luna",)},
    "deepseek": {"top": ("pro",), "large": ("pro",), "small": ("flash",), "mini": ("flash",)},
    # Owner fit, corrected 2026-11-20 (Argon dropped): Flash for lookups and small
    # edits (mini -> ``-medium``), Hydrogen medium for reads and traces (small),
    # Hydrogen high for bounded builds and Neon high to step up or plan (large,
    # ``-high``); Neon medium is the small step-up. Claude ids agy also serves are
    # still Anthropic, not this vendor, and match no family here.
    "antigravity": {
        "large": ("hydrogen", "neon"),
        "small": ("hydrogen", "neon"),
        "mini": ("flash",),
    },
}

#: Families never offered for a vendor, whatever the adapter advertises or the
#: owner pins (operator ruling 2026-11-20: Antigravity Argon is dropped).
DROPPED_FAMILIES: dict[str, tuple[str, ...]] = {"antigravity": ("argon",)}


def _dropped(vendor: str, model_id: str) -> bool:
    fams = DROPPED_FAMILIES.get(vendor)
    return fams is not None and _family_rank(model_id, fams) is not None


#: How a vendor spells a reasoning effort on an id: ``gpt-5.6-sol[high]`` (bracket)
#: or ``gemini-4-hydrogen-medium`` (dash). Unlisted vendors use brackets.
EFFORT_STYLE: dict[str, str] = {"antigravity": "dash"}

#: The router roles the local fleet is offered as (``/fleet/status`` ``roles``):
#: ``subagent`` is the CPU lane, ``agent`` the GPU lane.
LOCAL_ROLES: tuple[str, ...] = ("subagent", "agent")

#: Vendors offered to a parent of ANOTHER vendor. DeepSeek is metered (AGENTS.md:
#: a metered seat is only ever an explicitly named row), so it is never switched to.
CROSS_VENDORS: tuple[str, ...] = ("claude", "codex", "antigravity", "local")

#: Codex advertises one id per reasoning effort (``gpt-5.6-sol[high]``); the
#: effort a tier runs at when the adapter offers no bare id.
TIER_EFFORT = {"top": "high", "large": "high", "small": "medium", "mini": "medium"}

_EFFORT_SUFFIX = re.compile(r"\[(low|medium|high|xhigh|max|ultra)\]$")
_DASH_EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")
_EXCLUDE = re.compile(r"vision|exp\b|-exp|preview|orchestrator", re.I)


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
        if not advertised and vendor == "antigravity":
            advertised = antigravity_advertised()
    dash = EFFORT_STYLE.get(vendor) == "dash"
    suffix = _DASH_EFFORT_SUFFIX if dash else _EFFORT_SUFFIX
    out: dict[str, list[str]] = {}
    for tier, fams in families.items():
        by_model: dict[str, list[str]] = {}
        ranks: dict[str, int] = {}
        for mid in advertised or []:
            if not isinstance(mid, str) or _EXCLUDE.search(mid) or _dropped(vendor, mid):
                continue
            rank = _family_rank(mid, fams)
            if rank is None:
                continue
            base = suffix.sub("", mid)
            by_model.setdefault(base, []).append(mid)
            ranks[base] = min(ranks.get(base, rank), rank)
        picked = []
        for base in sorted(by_model, key=lambda b: ranks[b]):
            ids = by_model[base]
            if base in ids:
                picked.append(base)
            else:
                effort = TIER_EFFORT[tier]
                want = f"{base}-{effort}" if dash else f"{base}[{effort}]"
                picked.append(want if want in ids else ids[0])
        out[tier] = picked
    return out


#: How long one ``agy models`` answer is reused, and how long a failed one is.
AGY_MODELS_TTL_S = 3600
AGY_MODELS_RETRY_S = 300
AGY_MODELS_TIMEOUT_S = 20
_agy_models_cache: tuple[float, list[str]] | None = None
_AGY_ID = re.compile(r"[a-z0-9][a-z0-9.\-]*")


def antigravity_advertised(now: float | None = None) -> list[str]:
    """The ids the signed-in Antigravity account serves. Never raises; ``[]`` unknown.

    The companion discovers them per session (``agy models``) and keeps them in
    memory, so nothing on disk lists them. Read in order: the quota file the
    companion writes (its ``models`` keys), else one ``agy models`` run, cached.
    """
    global _agy_models_cache
    now = time.time() if now is None else now
    try:
        from kiro_crew import vendor_limits

        quota = vendor_limits.antigravity_health(now=now)
    except Exception:
        quota = {}
    if quota.get("per_model"):
        return list(quota["per_model"])
    if _agy_models_cache is not None:
        at, ids = _agy_models_cache
        if now - at < (AGY_MODELS_TTL_S if ids else AGY_MODELS_RETRY_S):
            return list(ids)
    ids = _run_agy_models()
    _agy_models_cache = (now, ids)
    return list(ids)


def _run_agy_models() -> list[str]:
    import os
    import shutil
    import subprocess

    command = (
        os.environ.get("ANTIGRAVITY_CLI")
        or shutil.which("agy")
        or str(Path.home() / ".local" / "bin" / "agy")
    )
    if not Path(command).is_file():
        return []
    try:
        done = subprocess.run(
            [command, "models"],
            capture_output=True,
            text=True,
            timeout=AGY_MODELS_TIMEOUT_S,
            check=False,
        )
    except Exception:
        logger.debug("spawn.route: agy models failed", exc_info=True)
        return []
    if done.returncode != 0:
        return []
    ids: list[str] = []
    for line in done.stdout.splitlines():
        head = line.split("\t", 1)[0].strip()
        if "\t" in line and _AGY_ID.fullmatch(head) and head not in ids:
            ids.append(head)
    return ids


def effective_tiers(vendor: str, config: Any | None = None) -> dict[str, list[str]]:
    """The owner's ``decisions.spawn_route`` pins for *vendor* where set, else the advertised tiers.

    Per TIER: a tier the owner pinned uses the pins, an unpinned tier falls back to
    what the adapter advertises, so a partial override does not blank the rest.
    """
    pinned = tier_map(config).get(vendor, {})
    derived = advertised_tiers(vendor)
    return {
        tier: [m for m in (pinned.get(tier) or derived.get(tier) or []) if not _dropped(vendor, m)]
        for tier in TIERS
    }


def candidates_for(
    vendor: str, mapping: Mapping[str, Mapping[str, list[str]]], backend: str = ""
) -> list[dict[str, str]]:
    """The offered options: one per pinned model id of *vendor*, keyed ``vendor/tier/i``.

    *backend* is what runs them: ``""`` for the parent's own seat.
    """
    out = []
    for tier in TIERS:
        for i, model in enumerate(mapping.get(vendor, {}).get(tier, [])):
            out.append(
                {
                    "key": f"{vendor}/{tier}/{i}",
                    "vendor": vendor,
                    "tier": tier,
                    "model": model,
                    "backend": backend,
                }
            )
    return out


def local_candidates(backends: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    """The local fleet as its router roles, one per role that has a backend (``local/<role>``)."""
    out = []
    for role in LOCAL_ROLES:
        spec = backends.get(f"local/{role}")
        if spec is None:
            continue
        out.append(
            {
                "key": f"local/{role}/0",
                "vendor": "local",
                "tier": role,
                "model": role,
                "backend": spec["backend"],
            }
        )
    return out


def _local_role_of_seat(seat_id: str) -> str:
    """The router role an execution seat of the local fleet serves: a background lane is ``subagent``."""
    low = seat_id.lower()
    return "subagent" if ("background" in low or low.endswith("-bg")) else "agent"


def route_backends(parent_vendor: str, cfg: Any | None = None) -> dict[str, dict[str, str]]:
    """What runs a child of each OTHER vendor, keyed by vendor (``local/<role>`` for the fleet).

    Each value is ``{backend, runtime_agent, native}``: ``backend`` is the id
    ``spawn_run(backend=...)`` accepts; a companion execution seat sets
    ``runtime_agent`` (its template alias), a native ACP backend sets ``native``
    (the backend id the factory takes). An execution seat wins over a native
    backend for the same vendor: it carries the operator's engine row (its
    environment and account), the native backend does not. The parent's own
    vendor is ``backend == ""``. Never raises; an unreadable source offers less.
    """
    out: dict[str, dict[str, str]] = {}
    try:
        from kiro_crew.execution_seats import declared_execution_seats

        seats = declared_execution_seats(cfg)
    except Exception:
        logger.debug("spawn.route: execution seats unreadable", exc_info=True)
        seats = {}
    for seat_id, seat in sorted(seats.items()):
        vendor = vendor_of_seat(seat_id) or vendor_of_seat(seat.get("runtime_agent", ""))
        if vendor not in CROSS_VENDORS or vendor == parent_vendor:
            continue
        key = f"local/{_local_role_of_seat(seat_id)}" if vendor == "local" else vendor
        out.setdefault(
            key, {"backend": seat_id, "runtime_agent": seat["runtime_agent"], "native": ""}
        )
    try:
        from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND, selectable_backend_values

        selectable = set(selectable_backend_values())
        native = {name: b for b, name in POLICY_ID_BY_BACKEND.items() if b in selectable}
    except Exception:
        logger.debug("spawn.route: native backends unreadable", exc_info=True)
        native = {}
    for vendor in ("claude", "codex"):
        if vendor == parent_vendor or vendor in out or vendor not in native:
            continue
        out[vendor] = {"backend": vendor, "runtime_agent": "", "native": native[vendor]}
    return out


def all_candidates(
    parent_vendor: str,
    config: Any | None = None,
    *,
    cross_vendor: bool = True,
    backends: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, str]]:
    """The parent's vendor's tiers on its own seat, then every other runnable vendor's.

    Without *cross_vendor* (a pinned backend, seat, crew or agent) only the
    parent's vendor is offered, exactly as before 2026-11-20. A parent on the
    local fleet is offered its roles on its own seat.
    """
    cands: list[dict[str, str]] = []
    if parent_vendor == "local":
        cands += local_candidates({f"local/{r}": {"backend": ""} for r in LOCAL_ROLES})
    else:
        cands += candidates_for(
            parent_vendor, {parent_vendor: effective_tiers(parent_vendor, config)}
        )
    if not cross_vendor:
        return cands
    if backends is None:
        backends = route_backends(parent_vendor)
    for vendor in CROSS_VENDORS:
        if vendor == parent_vendor:
            continue
        if vendor == "local":
            cands += local_candidates(backends)
            continue
        spec = backends.get(vendor)
        if spec is None:
            continue
        cands += candidates_for(
            vendor, {vendor: effective_tiers(vendor, config)}, backend=spec["backend"]
        )
    return cands


#: How long the limit readings (files, the fleet router) are reused.
LIMITS_CACHE_S = 30
_limits_cache: tuple[float, tuple[str, ...], dict[str, dict[str, Any]]] | None = None


def limit_health(
    antigravity_models: Sequence[str] = (), now: float | None = None
) -> dict[str, dict[str, Any]]:
    """Per vendor, its headroom reading (:mod:`kiro_crew.vendor_limits`); unknown vendors absent."""
    global _limits_cache
    now = time.time() if now is None else now
    key = tuple(antigravity_models)
    if (
        _limits_cache is not None
        and _limits_cache[1] == key
        and now - _limits_cache[0] < LIMITS_CACHE_S
    ):
        return _limits_cache[2]
    from kiro_crew import vendor_limits as vl

    out: dict[str, dict[str, Any]] = {}
    for vendor, read in (
        ("claude", lambda: vl.claude_health(now)),
        ("codex", vl.codex_health),
        ("antigravity", lambda: vl.antigravity_health(list(key), now=now)),
        ("local", vl.local_health),
    ):
        try:
            state = read()
        except Exception:
            logger.debug("spawn.route: %s limits unreadable", vendor, exc_info=True)
            state = {}
        if state:
            out[vendor] = state
    _limits_cache = (now, key, out)
    return out


def merged_health(
    tombstones: Mapping[str, Mapping[str, Any]], limits: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The tombstone outage and the live headroom, per vendor: out when either says so."""
    out: dict[str, dict[str, Any]] = {}
    for vendor in set(tombstones) | set(limits):
        t = tombstones.get(vendor) or {}
        lim = dict(limits.get(vendor) or {})
        state: dict[str, Any] = {
            "out": bool(t.get("out") or lim.get("out")),
            "until": t.get("until") if t.get("out") else None,
            "probe_after": t.get("probe_after") if t.get("out") else None,
        }
        lim.pop("out", None)
        state.update(lim)
        if state["out"] and state["until"] is None and lim.get("resets_at") is not None:
            state["until"] = lim["resets_at"]
        out[vendor] = state
    return out


def questions(cands: list[dict[str, str]]) -> list[Question]:
    return [
        Choice(
            QUESTION_ID,
            "A parent agent is delegating this brief to a sub-agent. Which of the configured "
            "vendor model rungs should run it (a candidate with a backend runs on that vendor, "
            "keeping the parent's memory)? top = a supervisor that orchestrates other agents or "
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
    has_check: bool = False,
    include_context: bool = True,
    estimate: Mapping[str, int] | None = None,
    limits: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    from kiro_crew import quota_rules
    from kiro_crew.subagent_manager.local_check import estimate_tokens

    return {
        "brief": (brief or "")[:MAX_BRIEF_CHARS],
        # Local-first gating inputs (fork, ``subagent_manager.local_check``): a
        # run with a machine check can be tried locally and falls through to
        # cloud when the check fails. ``estimate_tokens`` is per local router
        # role: the FULL brief plus that seat's measured first-request overhead,
        # to read against ``health.local.ctx`` for the same role.
        "has_check": bool(has_check),
        # *estimate* is the fitted figure (``fit_local``: after any context trim)
        # when the caller computed one.
        "estimate_tokens": (
            dict(estimate)
            if estimate is not None
            else estimate_tokens(brief or "", include_context=include_context)
        ),
        # the child's seat: a conductor/orchestrator agent is itself the orchestration cue
        "agent": agent or "",
        "parent_vendor": vendor,
        "parent_model": parent_model,
        "candidates": [dict(c) for c in cands],
        # A vendor with nothing known is absent (unknown -> usable); one with a
        # reading or an outage carries the contract's keys.
        "health": {
            v: {
                "out": bool(s.get("out")),
                "until": s.get("until"),
                "probe_after": s.get("probe_after"),
                **{k: s[k] for k in _HEALTH_KEYS if s.get(k) is not None},
            }
            for v, s in health.items()
            if s.get("out") or any(s.get(k) is not None for k in _HEALTH_KEYS)
        },
        # The one quota rule set (``quota_rules``, ``decisions.quota_limits``): the
        # decision server judges "low" and "out" with the same numbers KiroCrew did.
        "limits": dict(limits) if limits is not None else quota_rules.rules().as_state(),
    }


_HEALTH_KEYS = (
    "used_pct",
    "window",
    "resets_at",
    "per_model",
    "lanes",
    "ctx",
    "cpu_busy",
    "source",
    "as_of",
)


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


def drop_quota_outs(
    cands: Sequence[Mapping[str, str]],
    outs: Mapping[str, Mapping[str, Any]],
    exclude_vendors: Sequence[str] = (),
) -> list[dict[str, str]]:
    """*cands* minus every candidate a live quota mark covers, and every excluded vendor.

    A quota mark is a run's own proof that the vendor (or, for Antigravity, the
    model's group) refused work until its reset, so it is a hard filter, not a
    hint for the oracle: an offered candidate that is known out would be
    answered and fail again (quota fall-through, operator ruling 2026-11-20).
    """
    excluded = set(exclude_vendors)
    kept: list[dict[str, str]] = []
    for c in cands:
        v = c.get("vendor", "")
        if v in excluded:
            continue
        mark = outs.get(v) or {}
        if mark.get("until"):
            continue
        if c.get("model", "") in (mark.get("models") or {}):
            continue
        kept.append(dict(c))
    return kept


def _apply_quota_outs(
    health: dict[str, dict[str, Any]], outs: Mapping[str, Mapping[str, Any]]
) -> None:
    """A whole-vendor quota mark reads as ``out`` until its reset in *health*."""
    for vendor, mark in outs.items():
        until = mark.get("until")
        if not until:
            continue
        state = health.setdefault(vendor, {"out": False, "until": None, "probe_after": None})
        state["out"] = True
        state["until"] = max(float(state.get("until") or 0), float(until))
        state["source"] = "quota_hit"


async def routed_spawn(
    brief: str,
    *,
    agent: str,
    parent_model: str = "",
    session_key: str | None = None,
    config: Any | None = None,
    cross_vendor: bool = True,
    exclude_vendors: Sequence[str] = (),
    has_check: bool = False,
    context_groups: frozenset[str] | None = None,
    pinned_groups: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """The model -- and, for another vendor, the backend -- a model-less spawn should run on.

    ``None`` leaves the seat's default. Otherwise ``{route_id, model, tier, vendor,
    backend, runtime_agent, native_backend, p, reason, vendor_out, vendor_until,
    limit, low, out, latency_ms, trim, trim_budget, too_big}``: ``backend`` is ``""``
    for the parent's own seat;
    for another vendor exactly one of ``runtime_agent`` (an execution seat's alias)
    and ``native_backend`` (a native ACP backend id) is set. ``reason`` is the
    provider's own one-line account when it sent one.

    A local candidate is fitted to its lane first (:func:`fit_local_candidates`):
    *context_groups* (the groups the run keeps, ``None`` = all) are trimmed
    memory-then-lessons to fit, never a group in *pinned_groups*; a candidate
    whose minimal set still does not fit is skipped (``too_big``). ``trim`` is
    what the chosen local candidate drops (empty for every cloud pick). Never
    raises except :class:`asyncio.CancelledError`.
    """
    started = time.monotonic()
    route_id = uuid.uuid4().hex[:16]
    try:
        vendor = vendor_of_seat(agent, parent_model)
        if not vendor:
            return None
        backends = await asyncio.to_thread(route_backends, vendor) if cross_vendor else {}
        cands = await asyncio.to_thread(
            all_candidates, vendor, config, cross_vendor=cross_vendor, backends=backends
        )
        from kiro_crew import vendor_limits as _vl

        outs = await asyncio.to_thread(_vl.quota_outs)
        cands = drop_quota_outs(cands, outs, exclude_vendors)
        if not cands:
            return None
        ag_models = [c["model"] for c in cands if c["vendor"] == "antigravity"]
        tombs = await asyncio.to_thread(cached_health)
        limits = await asyncio.to_thread(limit_health, ag_models)
        health = merged_health(tombs, limits)
        _apply_quota_outs(health, outs)
        cands, fits, too_big = fit_local_candidates(
            cands, brief, health, groups=context_groups, pinned=pinned_groups
        )
        if not cands:
            return None
        state = build_state(
            brief,
            agent=agent,
            vendor=vendor,
            parent_model=parent_model,
            cands=cands,
            health=health,
            has_check=has_check,
            include_context=context_groups is None or bool(context_groups),
            estimate=_fitted_estimate(brief, fits, context_groups),
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
                "vendors": sorted({c["vendor"] for c in cands}),
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
    backend = chosen.get("backend") or ""
    spec: Mapping[str, str] = {}
    if backend:
        key = f"local/{chosen['tier']}" if chosen["vendor"] == "local" else chosen["vendor"]
        spec = backends.get(key) or {}
        if spec.get("backend") != backend:
            return None
    vendor_state = health.get(chosen["vendor"], {})
    fit = fits.get(chosen["tier"]) if chosen["vendor"] == "local" else None
    return {
        "route_id": route_id,
        "model": chosen["model"],
        "tier": chosen["tier"],
        "vendor": chosen["vendor"],
        "backend": backend,
        "runtime_agent": spec.get("runtime_agent", ""),
        "native_backend": spec.get("native", ""),
        "p": p,
        "reason": reason if isinstance(reason, str) else "",
        "vendor_out": bool(vendor_state.get("out")),
        "vendor_until": vendor_state.get("until"),
        "limit": _limit_summary(chosen, vendor_state),
        "low": sorted(v for v, st in health.items() if not st.get("out") and _is_low(st)),
        "out": sorted(v for v, st in health.items() if st.get("out")),
        "latency_ms": int((time.monotonic() - started) * 1000),
        # Never set for a cloud pick: a cloud window carries the full context.
        "trim": list(fit.drop) if fit is not None else [],
        "trim_budget": fit.budget if fit is not None else 0,
        "too_big": too_big,
    }


def fit_local_candidates(
    cands: Sequence[dict[str, str]],
    brief: str,
    health: Mapping[str, Mapping[str, Any]],
    *,
    groups: frozenset[str] | None = None,
    pinned: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, str]], dict[str, Any], list[str]]:
    """Fit each local candidate to its lane: ``(kept cands, {role: LocalFit}, too-big roles)``.

    A local candidate's run keeps *groups* minus what :func:`local_check.fit_local`
    drops to fit ``health.local.ctx[role]`` (or the role's known window). One
    whose minimal set (skills, project, spec, and every *pinned* group) still
    does not fit is removed, so the oracle only sees candidates that can carry
    the run. Cloud candidates pass through untouched.
    """
    from kiro_crew.subagent_manager.local_check import fit_local

    local_ctx = (health.get("local") or {}).get("ctx")
    ctx_by_role = local_ctx if isinstance(local_ctx, Mapping) else {}
    kept: list[dict[str, str]] = []
    fits: dict[str, Any] = {}
    too_big: list[str] = []
    for c in cands:
        if c.get("vendor") != "local":
            kept.append(c)
            continue
        role = c.get("tier") or ""
        fit = fit_local(role, brief, groups=groups, pinned=pinned, ctx=ctx_by_role.get(role))
        if fit.fits:
            fits[role] = fit
            kept.append(c)
        else:
            too_big.append(role)
    return kept, fits, too_big


def _fitted_estimate(
    brief: str, fits: Mapping[str, Any], groups: frozenset[str] | None
) -> dict[str, int] | None:
    """``estimate_tokens`` for the state with each fitted role at its trimmed size."""
    if groups is None and not fits:
        return None
    from kiro_crew.subagent_manager import local_check as lc

    est = lc.estimate_tokens(brief, groups=groups)
    for role, fit in fits.items():
        est[role] = (
            lc.brief_tokens(brief) + lc.LOCAL_HARNESS_OVERHEAD_TOKENS.get(role, 0) + fit.need
        )
    return est


#: The tier a fall-through prefers when the oracle gives no answer: the
#: failed run's own, else ordinary bounded work.
FALLBACK_TIER = "small"


def fallback_choice(cands: Sequence[Mapping[str, str]], tier: str = "") -> dict[str, str] | None:
    """A deterministic pick for a quota fall-through the oracle did not answer.

    Another vendor first (the failed one is already excluded), at the failed
    run's *tier* when offered, else :data:`FALLBACK_TIER`, else the first offered.
    """
    if not cands:
        return None
    for want in (tier, FALLBACK_TIER):
        for c in cands:
            if want and c.get("tier") == want:
                return dict(c)
    return dict(cands[0])


async def reroute_after_quota(
    brief: str,
    *,
    agent: str,
    parent_model: str = "",
    session_key: str | None = None,
    exclude_vendors: Sequence[str],
    tier: str = "",
    config: Any | None = None,
    context_groups: frozenset[str] | None = None,
    pinned_groups: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Where a routed run whose vendor ran out continues, or ``None`` when every vendor is out.

    The oracle is asked first, with every vendor in *exclude_vendors* and every
    quota-marked candidate removed. A fall-through must not depend on the
    Decisions seam being sampled, so with no answer the pick is
    :func:`fallback_choice` over the same filtered candidates. Never raises
    except :class:`asyncio.CancelledError`.
    """
    routed = await routed_spawn(
        brief,
        agent=agent,
        parent_model=parent_model,
        session_key=session_key,
        config=config,
        cross_vendor=True,
        exclude_vendors=exclude_vendors,
        context_groups=context_groups,
        pinned_groups=pinned_groups,
    )
    if routed and routed.get("vendor") not in set(exclude_vendors):
        return routed
    started = time.monotonic()
    try:
        from kiro_crew import vendor_limits as _vl

        vendor = vendor_of_seat(agent, parent_model)
        if not vendor:
            return None
        backends = await asyncio.to_thread(route_backends, vendor)
        cands = await asyncio.to_thread(
            all_candidates, vendor, config, cross_vendor=True, backends=backends
        )
        outs = await asyncio.to_thread(_vl.quota_outs)
        cands = drop_quota_outs(cands, outs, exclude_vendors)
        # No live health on this path: each local role is fitted to its known window.
        cands, fits, too_big = fit_local_candidates(
            cands, brief, {}, groups=context_groups, pinned=pinned_groups
        )
        chosen = fallback_choice(cands, tier)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("spawn.route: no fall-through candidate", exc_info=True)
        return None
    if chosen is None:
        return None
    backend = chosen.get("backend") or ""
    spec: Mapping[str, str] = {}
    if backend:
        key = f"local/{chosen['tier']}" if chosen["vendor"] == "local" else chosen["vendor"]
        spec = backends.get(key) or {}
        if spec.get("backend") != backend:
            return None
    fit = fits.get(chosen["tier"]) if chosen["vendor"] == "local" else None
    return {
        "route_id": uuid.uuid4().hex[:16],
        "model": chosen["model"],
        "tier": chosen["tier"],
        "vendor": chosen["vendor"],
        "backend": backend,
        "runtime_agent": spec.get("runtime_agent", ""),
        "native_backend": spec.get("native", ""),
        "p": None,
        "reason": "quota fall-through (deterministic)",
        "vendor_out": False,
        "vendor_until": None,
        "limit": "",
        "low": [],
        "out": sorted(set(exclude_vendors) | {v for v, m in outs.items() if m.get("until")}),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "trim": list(fit.drop) if fit is not None else [],
        "trim_budget": fit.budget if fit is not None else 0,
        "too_big": too_big,
    }


def pins_vendor(info: Any, *, own_worker: bool = False) -> bool:
    """Whether the spawn already chose its vendor, so the route stays within it.

    A per-spawn ``backend`` or execution seat, a ``crew`` (another crewmate runs
    on its own harness), a named ``agent``, a crewmate's own worker (its kit is
    checked against its harness before this point) and a continuation (it keeps
    the transport its record has) all pin it.
    """
    if own_worker or getattr(info, "backend", None) is not None:
        return True
    return any(
        bool(getattr(info, name, ""))
        for name in ("runtime_agent", "crew", "agent", "conversation_key")
    )


def apply_route_backend(info: Any, routed: Mapping[str, Any], execution: Any = None) -> bool:
    """Put a cross-vendor route's backend on the run; ``False`` refuses the whole route.

    The parent's own seat (``backend == ""``) needs nothing. An execution seat sets
    ``info.runtime_agent`` after the same member check admission runs for a
    requested seat (``validate_member_execution_seat``); a native backend sets
    ``info.backend``. The run's memory identity (its execution context) is not
    touched: the seat changes the transport, not the memory owner. Blocking
    (config read): call it off the event loop. Never raises.
    """
    if not routed.get("backend"):
        return True
    if pins_vendor(info):
        return False
    try:
        if routed.get("runtime_agent"):
            if execution is not None:
                from kiro_crew.execution_seats import validate_member_execution_seat

                validate_member_execution_seat(routed["runtime_agent"], execution)
            info.runtime_agent = routed["runtime_agent"]
            return True
        native = routed.get("native_backend")
        if isinstance(native, str) and native:
            info.backend = native
            return True
    except Exception:
        logger.info(
            "spawn.route: %s cannot carry this run; keeping the seat's own model",
            routed.get("backend"),
            exc_info=True,
        )
    return False


def _is_low(state: Mapping[str, Any]) -> bool:
    from kiro_crew.quota_rules import is_low

    return is_low(state)


def _limit_summary(chosen: Mapping[str, str], state: Mapping[str, Any]) -> str:
    """The chosen vendor's headroom in a few words: ``42% of five_hour``, ``lanes up``, ``unknown``."""
    per_model = state.get("per_model")
    if isinstance(per_model, dict) and chosen.get("model") in per_model:
        return f"{per_model[chosen['model']]:.0f}% of its quota"
    lanes = state.get("lanes")
    if isinstance(lanes, dict):
        return f"{chosen.get('tier')} lane {'up' if lanes.get(chosen.get('tier')) else 'down'}"
    pct = state.get("used_pct")
    if isinstance(pct, (int, float)):
        return f"{pct:.0f}% of {state.get('window') or 'window'}"
    return "limits unknown"


def reason_line(routed: Mapping[str, Any]) -> str:
    """One line for the child's card: vendor, model, kind (the provider's note) and limit state.

    ``spawn.route: read/simple -> antigravity small (gemini-4-hydrogen-medium) on antigravity
    p=0.90; 30% of its quota; skipped low: codex``
    """
    r = routed.get("reason")
    head = f"{r[:80]} -> " if isinstance(r, str) and r else ""
    p = routed.get("p")
    line = f"spawn.route: {head}{routed.get('vendor')} {routed.get('tier')} ({routed.get('model')})"
    if routed.get("backend"):
        line += f" on {routed['backend']}"
    if isinstance(p, (int, float)):
        line += f" p={p:.2f}"
    if routed.get("limit"):
        line += f"; {routed['limit']}"
    if routed.get("vendor_out"):
        until = routed.get("vendor_until")
        when = _dt.datetime.fromtimestamp(until).astimezone().strftime("%H:%M") if until else "?"
        line += f"; vendor rate-limited until {when}"
    others_out = [v for v in routed.get("out") or () if v != routed.get("vendor")]
    others_low = [v for v in routed.get("low") or () if v != routed.get("vendor")]
    if others_out:
        line += f"; out: {', '.join(others_out)}"
    if others_low:
        line += f"; low: {', '.join(others_low)}"
    if routed.get("trim"):
        from kiro_crew.subagent_manager.local_check import trim_line_for

        line += f"; {trim_line_for(routed['trim'], int(routed.get('trim_budget') or 0))}"
    if routed.get("too_big"):
        line += f"; local {', '.join(routed['too_big'])} skipped: too big"
    return line
