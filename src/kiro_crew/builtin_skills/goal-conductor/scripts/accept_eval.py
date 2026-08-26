#!/usr/bin/env python3
"""Stable skill adapter for KiroCrew's product-owned acceptance evaluator.

The conductor NEVER judges whether a work item succeeded.  The product-owned
evaluator does, and this adapter only preserves the installed skill's stable
``python3 accept_eval.py`` interface.  It always invokes one fixed KiroCrew CLI
verb: no field from stdin contributes to a command or argv.

Usage:
    python3 accept_eval.py < items.json

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


def main() -> int:
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
