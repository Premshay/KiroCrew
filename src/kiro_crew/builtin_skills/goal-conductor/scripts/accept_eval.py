#!/usr/bin/env python3
"""Stable skill adapter for KiroCrew's product-owned acceptance evaluator.

The conductor NEVER judges whether a work item succeeded.  The product-owned
evaluator does, and this adapter only preserves the installed skill's stable
``python3 accept_eval.py`` interface.  It always invokes one fixed KiroCrew CLI
verb: no field from stdin contributes to a command or argv.

Usage:
    python3 accept_eval.py < items.json
    python3 accept_eval.py --help      # this block, on stderr, exit 2

The input arrives on STDIN. Invoked with nothing piped in (a terminal) or with
``-h``/``--help``, this script prints this block and exits 2 instead of
blocking on a read that would look like a hang.

stdin (JSON):
    {"items": [
        {"id": "item-1", "accept": {"kind": "pr_checks", "pr": 123,
                                     "repo": "owner/name"}},
        {"id": "item-2", "accept": {"kind": "file", "path": "/abs/path",
                                     "exists": true}},
        {"id": "item-3", "accept": {"kind": "human_approval"}}
    ]}

stdout (JSON):
    {"results": [{"id": "...", "verdict": "pass|fail|pending|refused|error",
                  "evidence": "..."}]}

Exit code: 0 when evaluation ran (verdicts carry the outcome); 2 on malformed
input. A per-item problem is a verdict, never a crash - one bad spec must not
hide the others' results.

The adapter is stdlib-only because a bundled skill may run under the system
``python3`` rather than KiroCrew's environment.  The product CLI then imports
the installed package and owns all evaluation semantics.
"""

import subprocess
import sys

_PRODUCT_COMMAND = ("kirocrew", "_acceptance-evaluate")


def _usage_text() -> str:
    """The Usage..exit-code part of the module docstring, verbatim.

    Sliced out of ``__doc__`` instead of duplicated, so help a caller reads can
    never drift from the contract documented above it. ``python -OO`` strips
    docstrings, hence the one-line fallback.
    """
    doc = __doc__ or ""
    start = doc.find("Usage:")
    end = doc.find("The adapter is stdlib-only")
    if start < 0 or end <= start:
        return "Usage: python3 accept_eval.py < items.json"
    return doc[start:end].rstrip()


def _stdin_is_a_tty() -> bool:
    """Is stdin a terminal? A closed or detached stdin counts as not one."""
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args or _stdin_is_a_tty():
        # Run with nothing piped in, the product evaluator's stdin read blocks
        # on a read that never completes: from a caller's side that is a tool
        # timeout, an approval spent, and no output - not "you forgot the
        # input". Say what the input is and stop, before any subprocess starts.
        # Exit 2 is the code malformed input already uses, so nothing that
        # pipes real input sees a new outcome.
        print(_usage_text(), file=sys.stderr)
        return 2
    try:
        return subprocess.run(
            _PRODUCT_COMMAND,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            shell=False,
            check=False,
        ).returncode
    except FileNotFoundError:
        print("acceptance evaluator unavailable: 'kirocrew' not found on PATH", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"acceptance evaluator unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
