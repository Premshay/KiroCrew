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
#: * ``subagent`` (``local-background``, qwen-fast, 32k): 7,070-7,080 over
#:   n=33 sessions since 2026-11-06 (14,478 before that harness trim).
#: * ``agent`` (``local-fleet``, qwen38-mtp on Noda, 128k): 24,895-24,982 on
#:   2026-11-20 (n=2, repo cwd); 14,843-15,742 for n=8 earlier in November.
#:   The latest, larger figure is used: an underestimate is the failure that costs.
LOCAL_HARNESS_OVERHEAD_TOKENS: dict[str, int] = {"subagent": 7080, "agent": 24982}

#: The KiroCrew context a session injects ahead of the request (memory,
#: lessons, project, skills index) when any include_* flag is on. Median of the
#: November local-fleet first messages (n=9: 7,808-40,011).
INJECTED_CONTEXT_TOKENS = 20797

#: Characters per token for a brief on the Qwen3 vocab: the 10th percentile of
#: 95 local-lane first messages (median 3.87). Low on purpose: it over-counts.
CHARS_PER_TOKEN = 3.2


def brief_tokens(brief: str) -> int:
    return int(len(brief or "") / CHARS_PER_TOKEN) + 1


def estimate_tokens(brief: str, *, include_context: bool = True) -> dict[str, int]:
    """Per local router role, the first request a local seat would send for *brief*."""
    base = brief_tokens(brief) + (INJECTED_CONTEXT_TOKENS if include_context else 0)
    return {role: base + over for role, over in LOCAL_HARNESS_OVERHEAD_TOKENS.items()}


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
            f"local check FAIL -> not switched (pinned run)"
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
