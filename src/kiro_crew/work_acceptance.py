"""Evaluate the typed acceptance conditions used by coordinated work.

This is product code, not a bundled-skill implementation.  The goal-conductor
skill invokes it through Kiro Crew's fixed internal CLI verb so installed copies
of that skill keep using the evaluator that shipped with the running product.

The security boundary is intentionally narrow: a work item can select a typed
acceptance *kind*, but it can never supply an argv array, command name, or shell
string.  Any subprocess command is built here from a fixed template.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

#: Binaries this module itself invokes.  This is an internal invariant, not a
#: model-facing allowlist: no acceptance spec can name a command at all.
_SELF_BUILT_COMMANDS = {"gh", "kirocrew"}

TIMEOUT_SECS = 300
EVIDENCE_TAIL_CHARS = 500

#: ``gh pr checks`` uses exit 8 while a check is still running (gh >= 2.30).
_GH_PENDING_EXIT = 8


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-EVIDENCE_TAIL_CHARS:] if len(text) > EVIDENCE_TAIL_CHARS else text


def _run(argv: list[str], cwd: str | None = None) -> tuple[str, str]:
    """Run one SCRIPT-BUILT check and map its exit into a verdict."""
    raw = str(argv[0])
    problem, proc = _exec(argv, cwd)
    if proc is None:
        return problem or ("error", "could not run")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode == 0:
        return ("pass", output or "exit 0")
    if raw == "gh" and proc.returncode == _GH_PENDING_EXIT:
        return ("pending", output or "checks still running")
    return ("fail", f"exit {proc.returncode}: {output}")


def _pr_is_draft(pr: int, repo: object = None) -> bool:
    """Is this pull request still a draft? ``True`` only on an unambiguous yes.

    Asked only to explain a pending check run. One-sided on purpose: gh absent,
    no such PR, an auth failure or a forge without the field all return
    ``False``, so a probe that cannot answer never turns a wait into a refusal.
    Ported from upstream's bundled evaluator (kirodotdev/KiroCrew#9946).
    """
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", str(repo)]
    argv += ["--json", "isDraft", "-q", ".isDraft"]
    try:
        proc = subprocess.run(  # noqa: S603 - argv is product-built; no shell
            argv,
            capture_output=True,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip().lower() == "true"


_GH_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")


_CHECK_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


_CHECK_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


_EDITION_EXIT_VERDICTS = {0: "pass", 1: "fail", 3: "needs_person", 4: "error", 8: "pending"}


_EDITION_USAGE_EXIT = 2


def _exec(argv, cwd=None):
    """Run one SCRIPT-BUILT argv without a shell; return ``(problem, proc)``.

    Exactly one half is not ``None``. ``problem`` is a ready
    ``(verdict, evidence)`` pair for the cases where no process ran at all.

    Every caller must pass an argv it constructed itself. The guard below is an
    internal invariant, not a spec gate: reaching it with something outside
    ``_SELF_BUILT_COMMANDS`` means a handler leaked spec input into an exec path,
    so it refuses rather than runs. It lives HERE, in the one exec seam, which
    is why adding a second invocation did not add a second way past it.
    """
    raw = str(argv[0])
    if raw not in _SELF_BUILT_COMMANDS:
        return (
            (
                "refused",
                f"evaluator bug: {raw!r} is not a command this script builds; "
                "no spec field may name a command",
            ),
            None,
        )
    try:
        proc = subprocess.run(  # noqa: S603 - argv array, no shell, script-built
            [str(a) for a in argv],
            cwd=cwd or None,
            shell=False,
            capture_output=True,
            text=True,
            # Pin the decode instead of inheriting the locale: on Windows the
            # default is the ANSI code page, which mangles a check's UTF-8
            # output, and `errors="replace"` keeps genuinely undecodable bytes
            # from raising - a check's OUTPUT must never turn its verdict into a
            # crash, since the evidence is only ever read by a human.
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return (("error", f"timed out after {TIMEOUT_SECS}s"), None)
    except FileNotFoundError:
        return (("error", f"{raw!r} not found on PATH"), None)
    except OSError as exc:
        return (("error", f"could not run: {exc}"), None)
    return (None, proc)


def _pr_merged(accept):
    """Has this GitHub pull request merged? ``MERGED`` passes.

    ``OPEN`` is ``pending``, because it can still merge; ``CLOSED`` fails,
    because a closed pull request that did not merge carries no merged code. A
    ``gh`` that cannot answer (no such PR, no auth) is an ``error``, never a
    ``fail``: it says nothing about the work.

    ``repo`` is required, unlike ``pr_checks``: without it ``gh`` resolves the
    number against whatever checkout the evaluator runs in.
    """
    pr = accept.get("pr")
    # bool is an int subclass; a non-positive number would reach gh as a flag.
    if not isinstance(pr, int) or isinstance(pr, bool) or pr < 1:
        return ("error", "pr_merged spec needs a positive integer pr")
    repo = accept.get("repo")
    if not isinstance(repo, str) or not _GH_REPO.fullmatch(repo):
        return ("error", "pr_merged spec needs a repo of the form owner/name")
    argv = ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "state", "-q", ".state"]
    problem, proc = _exec(argv)
    if proc is None:
        return problem or ("error", "could not run")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0:
        return ("error", f"could not read PR #{pr} in {repo}: exit {proc.returncode}: {output}")
    state = proc.stdout.strip().upper()
    if state == "MERGED":
        return ("pass", f"PR #{pr} in {repo} is merged")
    if state == "OPEN":
        return ("pending", f"PR #{pr} in {repo} is open and not merged yet")
    if state == "CLOSED":
        return ("fail", f"PR #{pr} in {repo} was closed without merging")
    return ("error", f"PR #{pr} in {repo} reported an unknown state: {output}")


def _kirocrew_on_path():
    """Name the ``kirocrew`` this evaluator runs, for evidence only.

    The argv stays the bare name, resolved from ``PATH`` by the operating
    system, so this never picks the binary; it only says which one ``PATH``
    holds. That one need not be the install the gateway runs from (a shim
    beside a virtual environment, say), and an answer of ``error`` is easier
    to act on when it names it.
    """
    found = shutil.which("kirocrew")
    return f"kirocrew on PATH is {found}" if found else "no kirocrew on PATH"


def _edition_report(stdout):
    """The one JSON object ``kirocrew acceptance-check --json`` prints, or ``None``."""
    try:
        report = json.loads(stdout or "")
    except (ValueError, RecursionError):
        return None
    return report if isinstance(report, dict) else None


def _edition_check(accept):
    """Has an edition's own check on this reference passed?

    The edition does the reading and judging; this handler validates the two
    fields, builds the one fixed argv, and maps the answer. The exit code and the
    report's ``verdict`` must agree, and the report must name the check and the
    reference that were asked about, before either is believed. Anything else is
    an ``error``: an unexpected exit is a broken environment, not a verdict on the
    work, so it must never read as ``fail`` - or, worse, as ``pass``.

    ``needs_person`` (exit 3) becomes ``refused``: the check says waiting cannot
    change its answer and a person decides, and the conductor surfaces a
    ``refused`` verdict to the user rather than polling it. ``error`` (exit 4)
    stays ``error``, carrying the edition's own reason: the check exists but
    could not answer, which is never a verdict on the work.
    """
    check = accept.get("check")
    if not isinstance(check, str) or not _CHECK_NAME.fullmatch(check):
        return (
            "error",
            "edition_check spec needs a check name: a lower-case letter, then up to 31 "
            "lower-case letters, digits or hyphens",
        )
    ref = accept.get("ref")
    if not isinstance(ref, str) or not _CHECK_REF.fullmatch(ref):
        return (
            "error",
            "edition_check spec needs a ref: a letter or digit, then up to 127 letters, "
            "digits, dots, underscores or hyphens",
        )
    problem, proc = _exec(["kirocrew", "acceptance-check", "--json", check, ref])
    if proc is None:
        return problem or ("error", "could not run")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode == _EDITION_USAGE_EXIT:
        return (
            "error",
            f"no installed edition provides the acceptance check {check!r}: "
            f"kirocrew acceptance-check exited 2 ({output or 'no output'}); "
            f"{_kirocrew_on_path()}",
        )
    expected = _EDITION_EXIT_VERDICTS.get(proc.returncode)
    report = _edition_report(proc.stdout)
    if (
        expected is None
        or report is None
        or report.get("verdict") != expected
        or report.get("check") != check
        or report.get("ref") != ref
    ):
        return (
            "error",
            f"kirocrew acceptance-check exit {proc.returncode} came without a matching "
            f"report for {check} {ref}: {output}; {_kirocrew_on_path()}",
        )
    summary = _tail(str(report.get("summary") or "")) or f"{check} {ref}: {expected}"
    if expected == "error":
        return ("error", f"{summary} -- the edition's check could not answer")
    if expected == "needs_person":
        return ("refused", f"{summary} -- waiting will not change this; a person decides")
    return (expected, summary)


def evaluate(item: dict[Any, Any]) -> tuple[str, str]:
    """Return one verdict for a durable work-item acceptance spec."""
    accept = item.get("accept") or {}
    kind = accept.get("kind")
    if kind == "pr_checks":
        pr = accept.get("pr")
        # ``bool`` is an ``int`` subclass, but ``true`` is not a PR number.
        if not isinstance(pr, int) or isinstance(pr, bool):
            return ("error", "pr_checks spec needs an integer pr")
        argv = ["gh", "pr", "checks", str(pr)]
        repo = accept.get("repo")
        if repo:
            argv += ["--repo", str(repo)]
        verdict, evidence = _run(argv)
        # The probe explains a PENDING run and nothing else: a green draft is a
        # met acceptance, and a failed run is already terminal. A stateless
        # evaluator cannot tell a draft-gated readiness check (never resolves)
        # from CI still running, and answers both the same on purpose -- a draft
        # is the author's own "not ready", so the next step is a person's.
        if verdict == "pending" and _pr_is_draft(pr, repo):
            return (
                "refused",
                f"PR #{pr} is a draft and its checks have not finished; a draft "
                "is not a review-ready PR, and where readiness is gated on the "
                "draft flag no further polling resolves it -- mark it ready for "
                "review or change the acceptance kind",
            )
        return (verdict, evidence)
    if kind == "pr_merged":
        return _pr_merged(accept)
    if kind == "edition_check":
        return _edition_check(accept)
    if kind == "file":
        path = accept.get("path")
        if not isinstance(path, str) or not path:
            return ("error", "file spec needs a path")
        exists = accept.get("exists", True)
        # Rejected rather than coerced, matching the ``pr`` guard above:
        # ``bool("false")`` is ``True``, so coercing would report a PASS for
        # exactly the state the spec asked to rule out.
        if not isinstance(exists, bool):
            return ("error", "file spec needs a boolean exists")
        want = exists
        have = Path(path).exists()
        verdict = "pass" if have == want else "fail"
        return (verdict, f"{path} {'exists' if have else 'does not exist'}")
    if kind == "human_approval":
        return ("pending", "awaiting human approval - not machine-checkable")
    if kind == "cmd":
        return (
            "refused",
            "the 'cmd' kind was removed: a spec may not name a command to run. "
            "Use 'pr_checks' for CI-backed acceptance (it covers 'the tests "
            "pass', since CI runs them), or ask for a new purpose-built kind",
        )
    return ("error", f"unknown accept kind {kind!r}")


def evaluate_payload(payload: object) -> list[dict[str, str]]:
    """Evaluate every item, preserving a verdict for malformed siblings."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a JSON array")
    results: list[dict[str, str]] = []
    for position, item in enumerate(items):
        item_id = f"#{position}"
        try:
            if not isinstance(item, dict):
                raise TypeError(f"item must be a JSON object, got {type(item).__name__}")
            item_id = str(item.get("id", item_id))
            verdict, evidence = evaluate(item)
        except Exception as exc:  # one bad item must not hide sibling verdicts
            verdict, evidence = "error", f"evaluator bug on this item: {exc}"
        results.append({"id": item_id, "verdict": verdict, "evidence": evidence})
    return results


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    """Run the stable JSON stdin/stdout interface used by bundled skills."""
    try:
        payload = json.load(stdin)
        results = evaluate_payload(payload)
    except (TypeError, ValueError):
        print(json.dumps({"error": 'stdin must be JSON: {"items": [...]}'}), file=stdout)
        return 2
    print(json.dumps({"results": results}, indent=2), file=stdout)
    return 0
