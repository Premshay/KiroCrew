"""Quota fall-through: a sub-agent whose vendor runs out continues elsewhere at once.

Fork (Nexus), operator ruling of 2026-11-20. On 2026-11-20 Antigravity's
individual quota ran out (first seen 11:19 IST in a chat, 11:39 IST in spawned
runs). Each spawned run got the error inside ~5 s -- the companion ends the turn
``stop_reason='refusal'`` with ``Antigravity provider fault: status ERROR:
Individual quota reached ... Resets in 2h23m58s`` -- and the run ended
``failed (refusal)``. Nothing learned from it: the tombstone's ``detail`` is
the generic stop text, so ``spawn.route``'s tombstone scan never saw a limit,
and the next spawn went to the same vendor and failed the same way.

This module turns such an ending into a decision:

1. :func:`detect` reads a failed run's error (and the tail of its partial) for
   the vendor's quota signal -- Claude ``rate_limit`` / a rejected
   ``rate_limit_event`` / 429-with-limit, Codex ``usageLimitExceeded``,
   Antigravity ``Individual quota reached`` / ``QUOTA_EXHAUSTED`` /
   ``RESOURCE_EXHAUSTED`` -- with its reset time.
2. The vendor (Antigravity: the model's quota group) is marked out in
   :mod:`kiro_crew.vendor_limits` until that reset, so ``spawn.route`` drops it
   for every new spawn (a hard filter, ``spawn_route.drop_quota_outs``).
3. A ROUTED run (no model, backend, seat, crew or agent pin) is continued by a
   successor on the next routed candidate (``spawn_route.reroute_after_quota``)
   with the same cwd and memory identity and a brief that is the original task
   plus a handoff (the failed run's transcript tail and its cwd's git state).
   One fall-through per vendor: the chain of vendors already out rides on the
   successor and is excluded; when nothing is left the run gives up and says so.
4. A PINNED run is never switched: it ends ``quota_exhausted`` naming vendor,
   model and reset time, so the parent decides.

A start on a vendor already known out never waits for the vendor to say so
again: :func:`known_out` fails it before its session is created, and the ending
then takes the same path (a "stall bounded by a timeout" of zero).

Dashboard chat sessions are never switched: that re-prefills their history. A
chat turn that fails on its quota only writes the mark (:func:`note_chat_quota`,
called from ``dashboard.chat_quota_out``), so later spawns avoid that vendor.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

QUOTA_ERROR_CODE = "quota_exhausted"
#: The marker a known-out start fails with (and :func:`detect` recognises).
KNOWN_OUT_MARK = "quota_exhausted:"
#: How much of a failed run's partial is read for the vendor's error line: the
#: provider fault is the last thing it streamed. Wider would read the work itself.
RESULT_TAIL_CHARS = 600
#: The transcript tail a successor is handed.
HANDOFF_TAIL_CHARS = 3000
GIT_TIMEOUT_S = 5.0
#: Bound on everything the fall-through does inside the run's ``finally``.
SWITCH_TIMEOUT_S = 90.0
#: A quota error that names no reset: the vendors' 5-hour rolling window.
DEFAULT_WINDOW_S = 5 * 3600

#: Each vendor's own quota signals; ``spawn_route.limit_hit`` reads tombstones with them too.
QUOTA_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "antigravity": (
        re.compile(r"Individual quota reached", re.I),
        re.compile(r"\bQUOTA_EXHAUSTED\b|\bRESOURCE_EXHAUSTED\b"),
        re.compile(r"\bquota (?:reached|exceeded|exhausted)\b", re.I),
    ),
    "codex": (re.compile(r"usageLimitExceeded|hit your usage limit", re.I),),
    "claude": (
        re.compile(r"""errorKind['"]?\s*[:=]\s*['"]?rate_limit""", re.I),
        re.compile(r"\brate_limit_error\b|\busage limit reached\b|\bhit your limit\b", re.I),
        re.compile(r"\b429\b[^\n]{0,80}\b(?:quota|limit)", re.I),
    ),
}
# Claude Code: "Claude AI usage limit reached|1795180000" / "resets 5pm" / "resets 5:30pm"
_EPOCH = re.compile(r"limit reached\|(\d{10})")
_RESETS_AT = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)


@dataclass(frozen=True)
class QuotaHit:
    vendor: str
    model: str
    until: float
    detail: str


def run_vendor(info: Any, model: str = "") -> str:
    """The vendor that actually served *info* (``spawn_route.served_vendor``).

    The seat (``agent``) is the weakest witness: a ``crew-claude`` run moved to
    the Antigravity seat by ``runtime_agent`` was served by Antigravity
    (2026-11-20 run 521d4974 is exactly that, and its tombstone was read as Claude).
    """
    from kiro_crew.decisions.points.spawn_route import served_vendor

    backend = getattr(info, "backend", None)
    return served_vendor(
        runtime_agent=str(getattr(info, "runtime_agent", "") or ""),
        provider=backend if isinstance(backend, str) else "",
        model=model,
        agent=str(getattr(info, "agent", "") or ""),
    )


def run_model(info: Any) -> str:
    for name in ("resolved_model", "requested_model", "model"):
        m = str(getattr(info, name, "") or "")
        if m and m != "auto":
            return m
    return ""


def _reset_from(detail: str, vendor: str, now: float) -> float:
    from kiro_crew.decisions.points.spawn_route import reset_time, resets_in

    rel = resets_in(detail, now)
    if rel is not None:
        return rel
    m = _EPOCH.search(detail)
    if m:
        return float(m.group(1))
    if vendor == "claude":
        m = _RESETS_AT.search(detail)
        if m:
            h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            t = (
                _dt.datetime.fromtimestamp(now)
                .astimezone()
                .replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
            )
            if t.timestamp() < now - 60:
                t += _dt.timedelta(days=1)
            return t.timestamp()
    return reset_time(detail, now, DEFAULT_WINDOW_S)


def detect(
    text: str,
    *,
    vendor: str,
    model: str = "",
    now: float | None = None,
    use_health: bool = True,
) -> QuotaHit | None:
    """The quota hit *text* (a failed run's error and partial tail) proves, or ``None``.

    Only *vendor*'s own signals count, so a run that merely quotes another
    vendor's error is not read as out. Claude's rejected ``rate_limit_event``
    (``vendor_limits``) is a hit even when the error text names none. A start
    refused as already known out (:data:`KNOWN_OUT_MARK`) keeps the mark it hit.
    *use_health* off reads the text alone (a chat turn's error).
    """
    from kiro_crew import vendor_limits as vl

    now = time.time() if now is None else now
    text = text or ""
    if KNOWN_OUT_MARK in text:
        mark = vl.quota_out(vendor, model, now=now)
        if mark:
            return QuotaHit(vendor, model, float(mark["until"]), str(mark.get("detail") or ""))
        return None
    for rx in QUOTA_PATTERNS.get(vendor, ()):
        m = rx.search(text)
        if m:
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            line = text[start : end if end >= 0 else len(text)].strip()[:300]
            return QuotaHit(vendor, model, _reset_from(text, vendor, now), line)
    if vendor == "claude" and use_health:
        state = vl.claude_health(now)
        if state.get("out") and state.get("resets_at"):
            return QuotaHit(
                vendor, model, float(state["resets_at"]), "rate_limit_event status rejected"
            )
    return None


def known_out(info: Any, model: str = "", now: float | None = None) -> QuotaHit | None:
    """The live quota mark covering the vendor (and model) *info* is about to start on."""
    from kiro_crew import vendor_limits as vl

    model = model or run_model(info)
    vendor = run_vendor(info, model)
    if not vendor:
        return None
    mark = vl.quota_out(vendor, model, now=now)
    if not mark:
        return None
    return QuotaHit(vendor, model, float(mark["until"]), str(mark.get("detail") or ""))


def note_chat_quota(
    text: str,
    *,
    runtime_agent: str = "",
    agent: str = "",
    provider: str = "",
    model: str = "",
    now: float | None = None,
) -> QuotaHit | None:
    """Mark the vendor out when a dashboard chat turn's error is its quota. Never raises.

    The chat itself is not switched (that re-prefills its history); only the
    mark is written, so every later spawn avoids that vendor (Antigravity: the
    model's quota group) until the reset. 2026-11-20 11:19 IST chat-2069 hit
    ``Individual quota reached`` on gemini-4-hydrogen-high and nothing learned
    from it. Only the text counts: a chat failing for another reason while
    Claude's ``rate_limit_event`` reads rejected marks nothing new.
    """
    try:
        from kiro_crew import vendor_limits as vl
        from kiro_crew.decisions.points.spawn_route import served_vendor

        vendor = served_vendor(
            runtime_agent=runtime_agent, provider=provider, model=model, agent=agent
        )
        if not vendor or not text:
            return None
        hit = detect(text, vendor=vendor, model=model, now=now, use_health=False)
        if hit is None:
            return None
        vl.note_quota_out(vendor, until=hit.until, model=model, detail=hit.detail, now=now)
        _forget_route_caches()
        logger.warning("Chat turn hit its quota: %s (%s)", describe(hit, now), hit.detail)
        return hit
    except Exception:
        logger.warning("Chat quota out-mark failed", exc_info=True)
        return None


def _clock(ts: float, now: float | None = None) -> str:
    t = _dt.datetime.fromtimestamp(ts).astimezone()
    n = _dt.datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    return t.strftime("%H:%M") if t.date() == n.date() else t.strftime("%b %d %H:%M")


def describe(hit: QuotaHit, now: float | None = None) -> str:
    """``antigravity gemini-4-hydrogen-high out until 17:33``."""
    who = f"{hit.vendor} {hit.model}".strip()
    return f"{who} out until {_clock(hit.until, now)}"


def git_state(cwd: str) -> str:
    """Branch, last commits and uncommitted changes of *cwd*; ``""`` outside a repo. Never raises."""
    if not cwd:
        return ""

    def run(*args: str) -> str:
        try:
            p = subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except Exception:
            return ""
        return p.stdout.strip() if p.returncode == 0 else ""

    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    if not branch:
        return ""
    head = run("log", "-3", "--oneline", "--no-decorate")
    status = run("status", "--short")
    lines = status.splitlines()
    if len(lines) > 40:
        status = "\n".join(lines[:40] + [f"... {len(lines) - 40} more"])
    parts = [f"cwd: {cwd}", f"branch: {branch}"]
    if head:
        parts.append("last commits:\n" + head)
    parts.append("uncommitted:\n" + status if status else "uncommitted: none")
    return "\n".join(parts)


def handoff_brief(
    task: str, *, failed_id: str, hit: QuotaHit, tail: str, git: str, now: float | None = None
) -> str:
    """The successor's brief: the original task, then what the failed run already did."""
    tail = (tail or "").strip()
    if len(tail) > HANDOFF_TAIL_CHARS:
        tail = "..." + tail[-HANDOFF_TAIL_CHARS:]
    lines = [
        task.rstrip(),
        "",
        "---",
        f"## Handoff (quota fall-through from run {failed_id})",
        f"Run {failed_id} stopped because {describe(hit, now)}. You continue the same task "
        "in the same working directory. Check what it already did before redoing anything; "
        "do not repeat side effects (commits, pushes, posts) it already made.",
    ]
    if git:
        lines += ["", "### Working tree now", "```", git, "```"]
    lines += ["", "### Its transcript tail"]
    lines += ["```", tail, "```"] if tail else ["(it produced no output before stopping)"]
    return "\n".join(lines)


def _eligible(manager: Any, info: Any) -> bool:
    return bool(
        info.done
        and info.error
        and not info.user_stopped
        and not info.reaped
        and not getattr(info, "_reap_started", False)
        and not getattr(info, "_recovering", False)
        and not getattr(info, "_ending_claimed", False)
        and not getattr(info, "_retried_as", "")
        and not getattr(info, "_continued_as", "")
        and not getattr(manager, "_shutting_down", False)
    )


def _forget_route_caches() -> None:
    from kiro_crew.decisions.points import spawn_route as sr

    sr._limits_cache = None
    sr._health_cache = None


def _parent_model(manager: Any, info: Any) -> str:
    if not info.parent_session_key:
        return ""
    try:
        from kiro_crew.llm_helpers import provider_active_model

        provider = manager._sessions.get_provider(info.parent_session_key)
        return provider_active_model(provider) if provider is not None else ""
    except Exception:
        return ""


async def handle_run_end(manager: Any, info: Any, now: float | None = None) -> QuotaHit | None:
    """Mark the vendor out and switch or end *info* when it failed on a quota.

    Called from the run's ``finally`` before its terminal report, so the report
    carries the outcome. Returns the hit it acted on, else ``None``.
    """
    if not _eligible(manager, info):
        return None
    model = run_model(info)
    vendor = run_vendor(info, model)
    if not vendor:
        return None
    text = f"{info.error}\n{(info.result or '')[-RESULT_TAIL_CHARS:]}"
    hit = await asyncio.to_thread(detect, text, vendor=vendor, model=model, now=now)
    if hit is None:
        return None
    from kiro_crew import vendor_limits as vl

    await asyncio.to_thread(
        vl.note_quota_out, vendor, until=hit.until, model=model, detail=hit.detail, now=now
    )
    _forget_route_caches()
    info.error_code = QUOTA_ERROR_CODE
    what = describe(hit, now)
    logger.warning("Subagent %s hit its quota: %s (%s)", info.id, what, hit.detail)
    if not info.quota_routable:
        info.error = (
            f"{QUOTA_ERROR_CODE}: {what}. The run was pinned (model, backend, seat, crew "
            f"or agent), so it was not switched. {hit.detail}"
        ).strip()
        return hit
    await _fall_through(manager, info, hit, now)
    return hit


async def _fall_through(manager: Any, info: Any, hit: QuotaHit, now: float | None) -> None:
    from kiro_crew.decisions.points import spawn_route as sr

    what = describe(hit, now)
    chain = tuple(dict.fromkeys((*info.quota_chain, hit.vendor)))
    owner = manager.claim_retry(info)
    if owner:
        info.error = f"{QUOTA_ERROR_CODE}: {what}; run {owner} already continues this task"
        return
    successor = None
    try:
        origin = info.quota_origin_task or info._raw_task or info.task
        routed = await sr.reroute_after_quota(
            origin,
            agent=info.agent or "",
            parent_model=_parent_model(manager, info),
            session_key=info.parent_session_key or None,
            exclude_vendors=chain,
            tier=info.route_tier,
        )
        if not routed:
            info.error = (
                f"{QUOTA_ERROR_CODE}: {what}; every routed vendor is out "
                f"({', '.join(chain)}), so the task was not continued. {hit.detail}"
            ).strip()
            return
        git = await asyncio.to_thread(git_state, info.cwd or "")
        brief = handoff_brief(
            origin,
            failed_id=info.id,
            hit=hit,
            tail=info.result if info.result != "_No response._" else "",
            git=git,
            now=now,
        )
        execution = info.execution_context
        if execution is None:
            from kiro_crew.subagent_persistence import read_run_execution

            execution = await asyncio.to_thread(read_run_execution, info.id)
        native = routed.get("native_backend") or ""
        successor = await manager.spawn_async(
            brief,
            parent_session_key=info.parent_session_key,
            agent=info.agent,
            max_turns=info.max_turns,
            cwd=info.cwd,
            model=routed["model"],
            backend=native or None,
            runtime_agent=routed.get("runtime_agent") or "",
            approval_mode=info.approval_mode or None,
            silent=info.silent,
            delegation=dict(info.delegation),
            include_memory=info.include_memory,
            include_lessons=info.include_lessons,
            include_project=info.include_project,
            memory_store=info.memory_store,
            crew=info.crew,
            label=info.label,
            app=execution.app,
            _memory_mode=execution.memory_mode,
            _execution_context=execution.to_record(),
            _depth=getattr(info, "depth", None),
            _root_session_key=getattr(info, "root_session_key", None),
        )
        if successor is None or (successor.done and successor.error):
            why = successor.error if successor is not None else "capacity reached"
            info.error = (
                f"{QUOTA_ERROR_CODE}: {what}; the fall-through to {routed['vendor']} "
                f"{routed['model']} could not start: {why}"
            )
            successor = None
            return
        switch = f"quota fall-through: {what} -> {routed['vendor']} {routed['model']}"
        successor.quota_routable = True
        successor.quota_chain = chain
        successor.quota_origin_task = origin
        successor.route_tier = routed.get("tier") or ""
        successor.route_reason = f"{switch}; {sr.reason_line(routed)}"
        info.route_reason = f"{info.route_reason}; {switch} (run {successor.id})".lstrip("; ")
        info.error = (
            f"{switch}: continuing as run {successor.id}, same cwd and memory. "
            f"Wait for that run's completion; do not re-spawn this task."
        )
        logger.warning("Subagent %s: %s as run %s", info.id, switch, successor.id)
    finally:
        manager.settle_retry(info, successor.id if successor is not None else None)


async def handle_run_end_guarded(manager: Any, info: Any) -> None:
    """:func:`handle_run_end` bounded and silent: it runs in the run's ``finally``.

    Like the result cap beside it (``_cap_unclaimed_result_impl``) it never
    raises, a cancellation included, so the terminal report below it is still
    spawned and the parent still hears how the run ended.
    """
    try:
        await asyncio.wait_for(handle_run_end(manager, info), timeout=SWITCH_TIMEOUT_S)
    except asyncio.CancelledError:
        logger.debug("quota fall-through for %s cut short by a cancel", info.id)
    except Exception:
        logger.warning("Subagent %s: quota fall-through failed", info.id, exc_info=True)
