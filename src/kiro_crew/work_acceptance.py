"""Evaluate the typed acceptance conditions used by coordinated work.

This is product code, not a bundled-skill implementation.  The goal-conductor
skill invokes it through KiroCrew's fixed internal CLI verb so installed copies
of that skill keep using the evaluator that shipped with the running product.

The security boundary is intentionally narrow: a work item can select a typed
acceptance *kind*, but it can never supply an argv array, command name, or shell
string.  Any subprocess command is built here from a fixed template.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

#: Binaries this module itself invokes.  This is an internal invariant, not a
#: model-facing allowlist: no acceptance spec can name a command at all.
_SELF_BUILT_COMMANDS = {"gh"}

TIMEOUT_SECS = 300
EVIDENCE_TAIL_CHARS = 500

#: ``gh pr checks`` uses exit 8 while a check is still running (gh >= 2.30).
_GH_PENDING_EXIT = 8


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-EVIDENCE_TAIL_CHARS:] if len(text) > EVIDENCE_TAIL_CHARS else text


def _run(argv: list[str], cwd: str | None = None) -> tuple[str, str]:
    """Run one product-built check without a shell.

    The guard is deliberately below the typed handlers.  Reaching it with an
    unknown binary means a handler leaked untrusted input into an execution
    path, so the evaluator refuses rather than extending its own authority.
    """
    raw = str(argv[0])
    if raw not in _SELF_BUILT_COMMANDS:
        return (
            "refused",
            f"evaluator bug: {raw!r} is not a command this script builds; "
            "no spec field may name a command",
        )
    try:
        proc = subprocess.run(  # noqa: S603 - argv is product-built; no shell
            [str(arg) for arg in argv],
            cwd=cwd or None,
            capture_output=True,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return ("error", f"timed out after {TIMEOUT_SECS}s")
    except FileNotFoundError:
        return ("error", f"{raw!r} not found on PATH")
    except OSError as exc:
        return ("error", f"could not run: {exc}")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode == 0:
        return ("pass", output or "exit 0")
    if raw == "gh" and proc.returncode == _GH_PENDING_EXIT:
        return ("pending", output or "checks still running")
    return ("fail", f"exit {proc.returncode}: {output}")


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
        return _run(argv)
    if kind == "file":
        path = accept.get("path")
        if not isinstance(path, str) or not path:
            return ("error", "file spec needs a path")
        want = bool(accept.get("exists", True))
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
    except (json.JSONDecodeError, TypeError, ValueError):
        print(json.dumps({"error": 'stdin must be JSON: {"items": [...]}'}), file=stdout)
        return 2
    print(json.dumps({"results": results}, indent=2), file=stdout)
    return 0
