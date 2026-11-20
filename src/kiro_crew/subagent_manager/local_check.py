"""Local-first with a machine check: a local run's work is kept only when a check passes.

Fork (Nexus). ``spawn.route`` may send a model-less spawn to the local fleet
(``local/subagent`` CPU, ``local/agent`` GPU). A local model is free but weaker,
so its output is accepted on evidence, not on its own say-so:

1. A spawn carries a ``check`` -- a shell command whose exit 0 is a pass --
   given as ``spawn_run(check=...)`` (kept in ``info.delegation['check']`` so
   it survives the queue, a restart and a successor) or, when the brief names
   one obviously, extracted from it (:func:`extract_check`: a pytest command or
   pytest node ids).
2. When a run served by the local fleet ends, whatever the outcome
   (:func:`handle_run_end`):

   * it ended cleanly and has a check -> the check runs in the run's cwd,
     bounded by :data:`CHECK_TIMEOUT_S`. PASS -> uncommitted changes the worker
     left are committed (message names the run and the check); never on a
     protected branch. FAIL -> ``local_check_failed``.
   * it ended on an error (context overflow, ``lane_busy``, a crash, a
     timeout) -> ``local_unusable``, check or not.
   * it ended cleanly with no check -> nothing: no check, no auto-commit.

3. ``local_check_failed`` / ``local_unusable`` on a ROUTED run (no model,
   backend, seat, crew or agent pin; ``info.quota_routable``) falls through
   ONCE to the next cloud candidate on the quota-switch successor path
   (:func:`quota_switch.spawn_successor`): same cwd, memory identity unchanged,
   a handoff brief with the task, the failed run's tail, the check output and
   the git state. ``local`` joins the successor's excluded chain, so it never
   comes back to the fleet. A pinned run is never switched; it reports.

The receipt line (``info.local_check_line``) is what the parent reads:
``local check: <cmd> PASS -> committed <sha>`` or
``local check FAIL -> <vendor> <model>``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import subprocess
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

#: How long a check may run. A local pytest suite on this host is minutes, not hours.
CHECK_TIMEOUT_S = 600.0
#: Everything this module does after a run ends: the check plus the fall-through.
HANDLE_TIMEOUT_S = CHECK_TIMEOUT_S + 120.0
GIT_TIMEOUT_S = 15.0
#: How much check output rides in the handoff and the log.
CHECK_OUTPUT_TAIL_CHARS = 2000
#: Branches a check pass never commits on (``AGENTS.md``: never straight on main).
PROTECTED_BRANCHES = frozenset({"main", "master", "mainline"})

REASON_CHECK_FAILED = "local_check_failed"
REASON_UNUSABLE = "local_unusable"

# ---------------------------------------------------------------------------
# Token estimate inputs for spawn.route (measured, not guessed)
# ---------------------------------------------------------------------------

#: What each local seat sends on its FIRST request beyond the first user
#: message: Claude Code's system prompt and tool schemas under the isolated
#: ``CLAUDE_CONFIG_DIR`` harness. Measured 2026-11-20 from the seats' own
#: transcripts (``~/.local/share/claude-local-lane/projects/*/*.jsonl``): the
#: first assistant turn's ``usage`` (input + cache read + cache creation) minus
#: the first user message tokenized with the served model's vocab
#: (``llama-tokenize``, Qwen3 tokenizer).
#:
#: * ``subagent`` (``local-background``, qwen-fast, 32k): the lean helper
#:   harness (:data:`LOCAL_HELPER_TOOLS`, project and local setting tiers):
#:   4,046 on 2026-11-20 (claude-agent-acp 0.107.0, Claude Code 2.1.325,
#:   counted by qwen-fast's own ``/v1/messages/count_tokens``): system prompt
#:   758, the four tool schemas 2,124, environment / model / CLAUDE.md / git /
#:   date / attribution reminders 1,149 with a repo cwd that carries a
#:   CLAUDE.md, rounded up. The full harness it replaces measured 15,153 on the
#:   same run (12 tool schemas 8,602; MCP tool, agent, skill and MCP
#:   instruction listings 4,123).
#: * ``agent`` (``local-fleet``, qwen38-mtp on Noda, 128k): 24,895-24,982 on
#:   2026-11-20 (n=2, repo cwd); 14,843-15,742 for n=8 earlier in November.
#:   The latest, larger figure is used: an underestimate is the failure that costs.
LOCAL_HARNESS_OVERHEAD_TOKENS: dict[str, int] = {"subagent": 4050, "agent": 24982}

#: The first-turn prompt a local role is trimmed to, harness included: a token
#: TARGET, not the window. The CPU lane prefills at ~100 tokens/s at 2k, 58 at
#: 10k and 35 at 22.5k (qwen-fast, 2026-11-20), so a first turn sized to "fits
#: the window" took 11+ minutes and the reaper killed it at 300 s; a run sized
#: to this target starts answering in about 2-3 minutes. Raising the lane's
#: window does not move it: at 64k everything "fit", and that switched the
#: window-only trim off. The GPU role has no target and keeps fitting its window.
LOCAL_FIRST_TURN_TARGET_TOKENS: dict[str, int] = {"subagent": 12000}

#: The KiroCrew context a session injects ahead of the request (memory,
#: lessons, project, skills index) when any include_* flag is on. Median of the
#: November local-fleet first messages (n=9: 7,808-40,011).
INJECTED_CONTEXT_TOKENS = 20797

#: Characters per token for a brief on the Qwen3 vocab: the 10th percentile of
#: 95 local-lane first messages (median 3.87). Low on purpose: it over-counts.
CHARS_PER_TOKEN = 3.2


def brief_tokens(brief: str) -> int:
    return int(len(brief or "") / CHARS_PER_TOKEN) + 1


#: What each switchable group that can be TRIMMED adds to the injected context.
#: Measured 2026-11-20 on the same local-lane first messages (n=20 with a
#: session context, ``~/.local/share/claude-local-lane/projects/*/*.jsonl``):
#: each section tokenized with the Qwen3 vocab (``llama-tokenize``) and
#: assigned to its group by its header -- memory = ``[Memory tools]``,
#: stable profile, semantic, episodic, activity, conversation history;
#: lessons = learned corrections, learned experience, user profile. Medians
#: of the sessions that carried the group (memory n=15: 83-8,780; lessons
#: n=18: 81-6,763). A median, not a high percentile: overstating what a drop
#: saves is the failure that costs (a "fits" that overflows the lane).
CONTEXT_GROUP_TOKENS: dict[str, int] = {"memory": 3867, "lessons": 5801}

#: The order groups are dropped to fit a local lane (operator ruling
#: 2026-11-20): recall/episodic memory first, then lessons. Skills, the project
#: group (AGENTS.md / steering) and the crew/seat spec are never trimmed.
TRIM_ORDER: tuple[str, ...] = ("memory", "lessons")

#: The injected context that stays when everything trimmable is dropped:
#: conduct, skills index, crew/seat spec and the project group.
KEPT_CONTEXT_TOKENS = INJECTED_CONTEXT_TOKENS - sum(CONTEXT_GROUP_TOKENS.values())

#: What the lean helper contract (:data:`LOCAL_HELPER_CONTRACT`) saves over the
#: shipped Kiro Crew contract (``config/prompt.md``) in the ``[AGENT SYSTEM
#: PROMPT]`` block: 7,152 tokens for the shipped contract's block on the
#: 2026-11-20 helper run against 190 for this one (qwen-fast count).
LEAN_CONTRACT_SAVED_TOKENS = 6962

#: Output tokens each local seat reserves per request
#: (``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` on ``local-background`` and
#: ``local-fleet`` in the engine map); the router admits input + reserve
#: against the lane window, so the reserve comes off the input budget.
LOCAL_OUTPUT_RESERVE_TOKENS = 8192

#: The window each role serves when ``health.local.ctx`` has no live reading:
#: qwen-fast on the CPU lane, the Noda agent lane.
LOCAL_CTX_FALLBACK: dict[str, int] = {"subagent": 32768, "agent": 131072}

_ALL_GROUPS = frozenset({"memory", "lessons", "project"})


def context_tokens(groups: frozenset[str] | None, role: str = "") -> int:
    """The injected KiroCrew context for a run keeping *groups* (``None`` = every group).

    A *role* that runs the lean helper contract (:data:`LEAN_CONTRACT_ROLES`)
    carries that contract in place of the shipped one.
    """
    kept = _ALL_GROUPS if groups is None else groups
    base = KEPT_CONTEXT_TOKENS - (LEAN_CONTRACT_SAVED_TOKENS if role in LEAN_CONTRACT_ROLES else 0)
    return base + sum(t for g, t in CONTEXT_GROUP_TOKENS.items() if g in kept)


def estimate_tokens(
    brief: str, *, include_context: bool = True, groups: frozenset[str] | None = None
) -> dict[str, int]:
    """Per local router role, the first request a local seat would send for *brief*.

    *groups* (the context groups the run keeps) sizes the injected context by
    group; without it the measured median whole is used, or nothing when
    *include_context* is false.
    """
    out = {}
    for role, over in LOCAL_HARNESS_OVERHEAD_TOKENS.items():
        if groups is not None:
            ctx = context_tokens(groups, role)
        elif include_context:
            ctx = INJECTED_CONTEXT_TOKENS - (
                LEAN_CONTRACT_SAVED_TOKENS if role in LEAN_CONTRACT_ROLES else 0
            )
        else:
            ctx = 0
        out[role] = brief_tokens(brief) + ctx + over
    return out


def input_budget(role: str, ctx: int | None = None) -> int:
    """What a local *role*'s first turn may take as input.

    Its window minus the output reserve, capped by the role's first-turn
    token target (:data:`LOCAL_FIRST_TURN_TARGET_TOKENS`) when it has one.
    """
    window = ctx if isinstance(ctx, int) and ctx > 0 else LOCAL_CTX_FALLBACK.get(role, 0)
    budget = max(0, window - LOCAL_OUTPUT_RESERVE_TOKENS)
    target = LOCAL_FIRST_TURN_TARGET_TOKENS.get(role)
    return min(budget, target) if target else budget


class LocalFit:
    """Whether a run fits a local *role*, and which context groups it drops to."""

    __slots__ = ("role", "fits", "drop", "budget", "need")

    def __init__(
        self, role: str, fits: bool, drop: tuple[str, ...], budget: int, need: int
    ) -> None:
        self.role = role
        self.fits = fits
        self.drop = drop
        self.budget = budget
        self.need = need

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"LocalFit({self.role!r}, fits={self.fits}, drop={self.drop}, "
            f"budget={self.budget}, need={self.need})"
        )


def fit_local(
    role: str,
    brief: str,
    *,
    groups: frozenset[str] | None = None,
    pinned: frozenset[str] = frozenset(),
    ctx: int | None = None,
) -> LocalFit:
    """Fit a run keeping *groups* to local *role*, trimming in :data:`TRIM_ORDER`.

    Budget = min(window - output reserve, the role's first-turn target) - the
    seat's measured harness overhead - the brief; the injected context must fit
    what is left. A group in *pinned*
    (the caller set its ``include_*`` true explicitly) is never dropped. When
    the minimal set still does not fit, ``fits`` is false: the candidate is
    skipped as too big rather than sent to overflow.
    """
    budget = input_budget(role, ctx)
    room = budget - LOCAL_HARNESS_OVERHEAD_TOKENS.get(role, 0) - brief_tokens(brief)
    kept = set(_ALL_GROUPS if groups is None else groups)
    drop: list[str] = []
    need = context_tokens(frozenset(kept), role)
    for group in TRIM_ORDER:
        if need <= room:
            break
        if group in kept and group not in pinned:
            kept.discard(group)
            drop.append(group)
            need = context_tokens(frozenset(kept), role)
    fits = need <= room
    return LocalFit(role, fits, tuple(drop) if fits else (), budget, need)


def trim_line_for(drop: list[str] | tuple[str, ...], budget: int) -> str:
    """The receipt clause for a trimmed local pick: ``local: trimmed memory, lessons to fit 24.6k``."""
    return f"local: trimmed {', '.join(drop)} to fit {budget / 1000:.1f}k"


#: ``info.delegation`` keys (``dict[str, str]``, comma-joined groups) so both
#: survive the queue, a restart and a successor like the rest of a delegation.
#: ``context_pinned``: groups the caller set ``include_*=true`` explicitly.
#: ``context_trimmed``: groups this run dropped to fit its local lane.
PINNED_KEY = "context_pinned"
TRIMMED_KEY = "context_trimmed"

_FLAG_OF = {"memory": "include_memory", "lessons": "include_lessons", "project": "include_project"}


def _groups_of(raw: object) -> frozenset[str]:
    return frozenset(g for g in str(raw or "").split(",") if g in _FLAG_OF)


def pinned_groups(info: Any) -> frozenset[str]:
    """The groups *info*'s caller asked for explicitly: never trimmed."""
    return _groups_of((getattr(info, "delegation", None) or {}).get(PINNED_KEY))


def trimmed_groups(info: Any) -> frozenset[str]:
    """The groups *info* dropped to fit a local lane (restored for a cloud successor)."""
    return _groups_of((getattr(info, "delegation", None) or {}).get(TRIMMED_KEY))


def spawn_delegation(body: Any, check: str) -> dict[str, Any]:
    """The ``delegation`` kwarg ``POST /api/spawn`` passes: the check and the pinned groups.

    A group is pinned when the body set its ``include_*`` to ``true`` itself
    (validation fills absent flags with the same value, so explicitness is
    read from the raw body): a local lane may trim an unpinned group to fit,
    never a pinned one (:func:`fit_local`).
    """
    delegation: dict[str, str] = {}
    if (check or "").strip():
        delegation["check"] = check.strip()
    if isinstance(body, dict):
        pinned = [g for g in sorted(_FLAG_OF) if body.get(_FLAG_OF[g]) is True]
        if pinned:
            delegation[PINNED_KEY] = ",".join(pinned)
    return {"delegation": delegation} if delegation else {}


def apply_trim(info: Any, drop: list[str] | tuple[str, ...]) -> None:
    """Drop *drop* from *info*'s context scope through its ``include_*`` flags.

    The same switch a parent's ``include_*=false`` throws, so the context
    builder, the ``state.json`` scope record and the harness memory switch all
    read one value. A pinned group is never dropped, whatever *drop* says.
    """
    pinned = pinned_groups(info)
    done = [g for g in drop if g in _FLAG_OF and g not in pinned]
    for g in done:
        setattr(info, _FLAG_OF[g], False)
    if done:
        delegation = dict(getattr(info, "delegation", None) or {})
        delegation[TRIMMED_KEY] = ",".join(sorted(trimmed_groups(info) | set(done)))
        info.delegation = delegation


# ---------------------------------------------------------------------------
# The lean helper profile (CPU-lane roles)
# ---------------------------------------------------------------------------

#: Local roles whose sub-agent runs take the lean helper profile: the shipped
#: Kiro Crew contract is replaced by :data:`LOCAL_HELPER_CONTRACT`, the
#: harness mounts only :data:`LOCAL_HELPER_TOOLS` and no MCP server, and the
#: injected context is trimmed to :data:`LOCAL_FIRST_TURN_TARGET_TOKENS`.
#: The GPU ``agent`` role is not one: it keeps the full contract and tools.
LEAN_CONTRACT_ROLES: frozenset[str] = frozenset({"subagent"})

#: The built-in tools a lean helper keeps: what a coding helper needs. Grep and
#: Glob stay (operator ruling 2026-11-20): searching code is how an agent finds
#: what to read, and their 1,567 tokens of schema are worth that.
LOCAL_HELPER_TOOLS: tuple[str, ...] = ("Bash", "Read", "Edit", "Write", "Grep", "Glob")

#: The Claude Code setting tiers a lean helper loads: the project's, as every
#: Crew session that binds MCP servers already does. The user tier stays out:
#: on the local lane its ``modelOverrides`` make Claude Code read the role name
#: as an older Claude model, which switches to that model's long system prompt
#: and tool descriptions (+4,463 tokens measured 2026-11-20).
LOCAL_HELPER_SETTING_SOURCES: tuple[str, ...] = ("project", "local")

#: The ``[AGENT SYSTEM PROMPT]`` a lean helper runs under, in place of the
#: shipped contract, whose sections teach Kiro Crew MCP tools, dashboard
#: widgets, browsing and orchestration that a lean helper does not mount.
LOCAL_HELPER_CONTRACT = """You are a Kiro Crew helper running on a local model. \
The agent that started you reads your reply; no person is watching.

- Do the task with the tools this session has (Read, Grep, Glob, Edit, Write, Bash). \
Kiro Crew's MCP tools (spawning, scheduling, memory, dashboards) are not \
mounted here: do not search for them or ask for them.
- Work in your working directory. Prefer small, direct steps. When the task \
names a check (tests, a build, a command), run it and fix failures before you finish.
- Do not ask questions. If something the task needs is missing (a file, a \
credential, context your parent withheld), say exactly what and stop rather than guess.
- Finish with a short report: the files you changed (absolute paths), the \
commands you ran and their results, and anything left undone."""

_LEAN_SESSIONS: OrderedDict[str, None] = OrderedDict()
_LEAN_SESSIONS_LOCK = threading.Lock()
#: Sessions remembered as lean, so a compaction's re-injected contract is the
#: one the session started with. Bounded: the oldest is forgotten first.
_LEAN_SESSIONS_MAX = 2048


def mark_lean_session(session_key: str) -> None:
    """Record *session_key* as a lean helper session (its contract is :data:`LOCAL_HELPER_CONTRACT`)."""
    if not session_key:
        return
    with _LEAN_SESSIONS_LOCK:
        _LEAN_SESSIONS[session_key] = None
        _LEAN_SESSIONS.move_to_end(session_key)
        while len(_LEAN_SESSIONS) > _LEAN_SESSIONS_MAX:
            _LEAN_SESSIONS.popitem(last=False)


def is_lean_session(session_key: str | None) -> bool:
    """Whether *session_key* runs the lean helper contract."""
    if not session_key:
        return False
    with _LEAN_SESSIONS_LOCK:
        return session_key in _LEAN_SESSIONS


def local_role_of_run(info: Any) -> str:
    """The local router role serving *info*, or ``""`` when the local fleet does not serve it.

    A run routed to the fleet carries its role as ``route_tier``; a run started
    on a companion execution seat (``spawn_run(backend="local-background")``)
    is matched to the seat whose template alias it runs under. Blocking (reads
    the config): call it off the event loop. Never raises.
    """
    if getattr(info, "route_vendor", "") == "local":
        tier = str(getattr(info, "route_tier", "") or "")
        return tier if tier in LOCAL_CTX_FALLBACK else ""
    runtime_agent = str(getattr(info, "runtime_agent", "") or "")
    if not runtime_agent:
        return ""
    try:
        from kiro_crew.decisions.points.spawn_route import _local_role_of_seat, vendor_of_seat
        from kiro_crew.execution_seats import declared_execution_seats

        seats = declared_execution_seats()
    except Exception:
        logger.debug("local role: execution seats unreadable", exc_info=True)
        return ""
    for seat_id, seat in sorted(seats.items()):
        if seat.get("runtime_agent") == runtime_agent and vendor_of_seat(seat_id) == "local":
            return _local_role_of_seat(seat_id)
    return ""


def lean_helper_profile() -> dict[str, list[str]]:
    """The ``local_helper_profile`` factory kwarg: the tools and setting tiers a lean helper keeps."""
    return {
        "tools": list(LOCAL_HELPER_TOOLS),
        "setting_sources": list(LOCAL_HELPER_SETTING_SOURCES),
    }


def fit_seat_run(info: Any, role: str) -> LocalFit:
    """Fit a run started on a local seat to *role*'s first-turn target, and trim it.

    The routed path fits before it picks (``spawn_route.fit_local_candidates``);
    a run that named its seat is fitted here instead. A group the caller asked
    for explicitly is never dropped, and a run whose minimal set still does not
    fit starts as it is: it chose its seat.
    """
    groups = frozenset(g for g, flag in _FLAG_OF.items() if getattr(info, flag, True))
    task = getattr(info, "_raw_task", "") or getattr(info, "task", "") or ""
    fit = fit_local(role, str(task), groups=groups, pinned=pinned_groups(info))
    if fit.drop:
        apply_trim(info, fit.drop)
    return fit


def prepare_lean_run(info: Any, session_key: str) -> dict[str, list[str]] | None:
    """Put a run on the CPU lane onto the lean helper profile; ``None`` for any other run.

    Fits a run that named its seat to the role's first-turn target (a routed
    pick was fitted before it was picked), records *session_key* as lean so its
    contract is :data:`LOCAL_HELPER_CONTRACT`, and returns the
    ``local_helper_profile`` the provider factory takes. Blocking (reads the
    config): call it off the event loop.
    """
    role = local_role_of_run(info)
    if role not in LEAN_CONTRACT_ROLES:
        return None
    if getattr(info, "route_vendor", "") != "local":
        fit = fit_seat_run(info, role)
        logger.info(
            "Subagent %s: lean %s helper, first turn fitted to %d tokens%s",
            getattr(info, "id", "?"),
            role,
            fit.budget,
            f" (trimmed {', '.join(fit.drop)})" if fit.drop else "",
        )
    mark_lean_session(session_key)
    return lean_helper_profile()


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

_PYTEST_CMD = re.compile(r"`((?:(?:uv run|\S*/)?python3? -m |\S*/|uv run )?pytest(?: [^`\n]*)?)`")
_NODE_ID = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*test_[\w-]*\.py(?:::[\w\[\]-]+(?:::[\w\[\]-]+)?)?)(?![\w/.-])"
)
#: Characters a check extracted from a brief may not contain: it is run by a
#: shell, and a brief is text an agent wrote.
_UNSAFE = re.compile(r"[;&|<>$`\\(){}!]")


def extract_check(brief: str) -> str:
    """The check a brief names obviously, else ``""``.

    Obvious means: exactly one backticked pytest command, or failing that,
    pytest node ids / test files the brief names (up to four), run as
    ``python -m pytest -q <ids>``. Anything with shell metacharacters is refused.
    """
    text = brief or ""
    cmds = list(dict.fromkeys(m.group(1).strip() for m in _PYTEST_CMD.finditer(text)))
    if cmds:
        # One command and it is clean; anything else (two, or a refused one) is not obvious.
        return cmds[0] if len(cmds) == 1 and not _UNSAFE.search(cmds[0]) else ""
    ids = list(dict.fromkeys(m.group(1) for m in _NODE_ID.finditer(text)))
    if not ids or len(ids) > 4 or any(_UNSAFE.search(i) for i in ids):
        return ""
    return "python -m pytest -q " + " ".join(shlex.quote(i) for i in ids)


def check_for(info: Any) -> str:
    """The run's check: the explicit one, else the brief's own."""
    explicit = str((getattr(info, "delegation", None) or {}).get("check") or "").strip()
    if explicit:
        return explicit
    task = (
        getattr(info, "quota_origin_task", "")
        or getattr(info, "_raw_task", "")
        or getattr(info, "task", "")
    )
    return extract_check(str(task or ""))


def run_check(cmd: str, cwd: str, timeout: float = CHECK_TIMEOUT_S) -> tuple[bool, str]:
    """(passed, output tail). A timeout or a spawn failure is a fail. Never raises."""
    try:
        p = subprocess.run(
            ["bash", "-c", cmd],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return False, f"{out[-CHECK_OUTPUT_TAIL_CHARS:]}\n(check timed out after {timeout:.0f}s)"
    except Exception as exc:  # cwd gone, bash missing
        return False, f"check could not run: {exc}"
    out = (p.stdout or "") + (p.stderr or "")
    tail = out[-CHECK_OUTPUT_TAIL_CHARS:]
    return p.returncode == 0, f"{tail}\n(exit {p.returncode})".strip()


def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except Exception as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout or p.stderr or "").strip()


def commit_changes(cwd: str, run_id: str, cmd: str) -> tuple[str, str]:
    """Commit what the worker left uncommitted: ``(sha, "")`` or ``("", why not)``. Never raises."""
    if not cwd:
        return "", "no cwd"
    rc, branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return "", "not a git work tree"
    rc, status = _git(cwd, "status", "--porcelain")
    if rc != 0:
        return "", f"git status failed: {status[:200]}"
    if not status:
        return "", "no uncommitted changes"
    if branch in PROTECTED_BRANCHES:
        return "", f"on protected branch {branch}; left uncommitted"
    rc, out = _git(cwd, "add", "-A")
    if rc != 0:
        return "", f"git add failed: {out[:200]}"
    msg = f"local run {run_id}: check passed\n\nCheck: {cmd}\nRun: {run_id}"
    rc, out = _git(cwd, "commit", "-q", "--no-verify", "-m", msg)
    if rc != 0:
        return "", f"git commit failed: {out[:200]}"
    rc, sha = _git(cwd, "rev-parse", "--short", "HEAD")
    return (sha, "") if rc == 0 else ("", "commit made, sha unreadable")


# ---------------------------------------------------------------------------
# The run's ending
# ---------------------------------------------------------------------------


def served_local(info: Any) -> bool:
    """Whether the local fleet served *info* (routed there, or its seat is local)."""
    if getattr(info, "route_vendor", "") == "local":
        return True
    from kiro_crew.subagent_manager.quota_switch import run_model, run_vendor

    try:
        return run_vendor(info, run_model(info)) == "local"
    except Exception:
        return False


def _eligible(manager: Any, info: Any) -> bool:
    return bool(
        info.done
        and not info.user_stopped
        and not info.reaped
        and not getattr(info, "_reap_started", False)
        and not getattr(info, "_recovering", False)
        and not getattr(info, "_ending_claimed", False)
        and not getattr(info, "_retried_as", "")
        and not getattr(info, "_continued_as", "")
        and not getattr(manager, "_shutting_down", False)
    )


def handoff_brief(
    task: str,
    *,
    failed_id: str,
    reason: str,
    why: str,
    tail: str,
    check: str,
    check_output: str,
    git: str,
) -> str:
    from kiro_crew.subagent_manager.quota_switch import HANDOFF_TAIL_CHARS

    tail = (tail or "").strip()
    if len(tail) > HANDOFF_TAIL_CHARS:
        tail = "..." + tail[-HANDOFF_TAIL_CHARS:]
    lines = [
        task.rstrip(),
        "",
        "---",
        f"## Handoff ({reason} from local run {failed_id})",
        f"Local run {failed_id} {why}. You continue the same task in the same working "
        "directory. Check what it already did before redoing anything; keep what is "
        "right, fix what is not, and do not repeat side effects it already made.",
    ]
    if check:
        lines += ["", f"### The check (must pass): `{check}`"]
        if check_output:
            lines += ["Its output on the local run's work:", "```", check_output.strip(), "```"]
    if git:
        lines += ["", "### Working tree now", "```", git, "```"]
    lines += ["", "### Its transcript tail"]
    lines += ["```", tail, "```"] if tail else ["(it produced no output)"]
    return "\n".join(lines)


async def handle_run_end(manager: Any, info: Any) -> str:
    """Check, commit or fall through a run the local fleet served. Returns the outcome.

    ``""`` (not a local run / nothing to do), ``"pass"``, ``"pass_uncommitted"``,
    or the fall-through reason (:data:`REASON_CHECK_FAILED`,
    :data:`REASON_UNUSABLE`), switched or not.
    """
    if not _eligible(manager, info) or not served_local(info):
        return ""
    cmd = check_for(info)
    errored = bool(info.error)
    if not errored and not cmd:
        return ""
    cwd = info.cwd or ""
    output = ""
    if not errored:
        passed, output = await asyncio.to_thread(run_check, cmd, cwd)
        if passed:
            sha, why_not = await asyncio.to_thread(commit_changes, cwd, info.id, cmd)
            if sha:
                info.local_check_line = f"local check: {cmd} PASS -> committed {sha}"
                outcome = "pass"
            else:
                info.local_check_line = f"local check: {cmd} PASS -> not committed ({why_not})"
                outcome = "pass_uncommitted"
            logger.info("Subagent %s: %s", info.id, info.local_check_line)
            return outcome
        reason = REASON_CHECK_FAILED
        why = f"finished, but its check `{cmd}` failed"
    else:
        reason = REASON_UNUSABLE
        why = f"ended unusable: {str(info.error)[:300]}"
    info.error_code = reason
    if not info.quota_routable:
        info.local_check_line = (
            "local check FAIL -> not switched (pinned run)"
            if reason == REASON_CHECK_FAILED
            else "local run unusable -> not switched (pinned run)"
        )
        if reason == REASON_CHECK_FAILED:
            info.error = (
                f"{reason}: the check `{cmd}` failed on the local run's work. The run was "
                f"pinned, so it was not switched.\n{output[-600:]}"
            ).strip()
        return reason
    await _fall_through(manager, info, reason=reason, why=why, cmd=cmd, output=output)
    return reason


async def _fall_through(
    manager: Any, info: Any, *, reason: str, why: str, cmd: str, output: str
) -> None:
    from kiro_crew.decisions.points import spawn_route as sr
    from kiro_crew.subagent_manager import quota_switch as qs

    chain = tuple(dict.fromkeys((*info.quota_chain, "local")))
    label = "local check FAIL" if reason == REASON_CHECK_FAILED else "local run unusable"
    owner = manager.claim_retry(info)
    if owner:
        info.local_check_line = f"{label} -> run {owner} already continues this task"
        info.error = f"{reason}: {why}; run {owner} already continues this task"
        return
    successor = None
    try:
        origin = info.quota_origin_task or info._raw_task or info.task
        routed = await sr.reroute_after_quota(
            origin,
            agent=info.agent or "",
            parent_model=qs._parent_model(manager, info),
            session_key=info.parent_session_key or None,
            exclude_vendors=chain,
            tier=sr.FALLBACK_TIER,
            **qs._local_scope(info),
        )
        if not routed:
            info.local_check_line = f"{label} -> no cloud candidate left"
            info.error = (
                f"{reason}: {why}; no cloud candidate is available, so it was not continued"
            )
            return
        git = await asyncio.to_thread(qs.git_state, info.cwd or "")
        result = info.result if info.result != "_No response._" else ""
        brief = handoff_brief(
            origin,
            failed_id=info.id,
            reason=reason,
            why=why,
            tail=result,
            check=cmd,
            check_output=output,
            git=git,
        )
        target = f"{routed['vendor']} {routed['model']}"
        switch = f"{reason}: {label} -> {target}"
        successor, refusal = await qs.spawn_successor(
            manager, info, routed, brief, chain=chain, origin=origin, switch=switch
        )
        if successor is None:
            info.local_check_line = f"{label} -> {target} could not start"
            info.error = f"{reason}: {why}; the fall-through to {target} could not start: {refusal}"
            return
        info.local_check_line = f"{label} -> {target}"
        info.error = (
            f"{switch}: continuing as run {successor.id}, same cwd and memory. "
            "Wait for that run's completion; do not re-spawn this task."
        )
        logger.warning("Subagent %s: %s as run %s", info.id, switch, successor.id)
    finally:
        manager.settle_retry(info, successor.id if successor is not None else None)


async def handle_run_end_guarded(manager: Any, info: Any) -> None:
    """:func:`handle_run_end` bounded and silent: it runs in the run's ``finally``."""
    try:
        await asyncio.wait_for(handle_run_end(manager, info), timeout=HANDLE_TIMEOUT_S)
    except asyncio.CancelledError:
        logger.debug("local check for %s cut short by a cancel", info.id)
    except Exception:
        logger.warning("Subagent %s: local check failed to run", info.id, exc_info=True)
