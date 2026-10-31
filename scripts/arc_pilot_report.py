#!/usr/bin/env python3
"""arc_pilot_report.py -- compare a pilot arc's coordination and product cost with a baseline.

Duplication is checked against each PR's ``baseRefOid`` read through git in ``--base-dir``
(a git checkout of the repo that holds that commit), so a merged PR never matches itself.
Board-derived rows read the arc store; PR-derived rows come from ``gh``. A baseline arc has
no board, so it is reported from ``--baseline-prs`` alone and its board rows print ``n/a``.

Usage::

    python3 scripts/arc_pilot_report.py --arc arc_<id> --since 2026-10-01T00:00:00+00:00 \\
        --until 2026-10-08T00:00:00+00:00 --base-dir /path/to/git/checkout --baseline-prs 12,13
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from kiro_crew import arcs

BLOCK = 5
NA = "n/a"
_PULL_RE = re.compile(r"/pull/(\d+)")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _aware(value: str) -> str:
    if _ts(value).tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} needs a UTC offset, e.g. 2026-10-01T00:00:00Z")
    return value


def _minutes(delta_secs: float) -> float:
    return round(delta_secs / 60, 1)


def _median_max(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"median": None, "max": None}
    return {"median": round(statistics.median(values), 1), "max": round(max(values), 1)}


def window_entries(arc_id: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    entries = arcs.read_entries(arc_id, limit=10**9)
    return [e for e in entries if since <= _ts(e["ts"]) < until]


def collision_count(entries: list[dict[str, Any]]) -> int:
    """Claims that, when written, matched another author's still-open claim."""
    open_claims: list[dict[str, Any]] = []
    count = 0
    for entry in entries:
        if entry["kind"] == "CLAIM":
            if arcs.collides(open_claims, entry):
                count += 1
            open_claims.append(entry)
        elif entry["kind"] in ("DONE", "FAIL") and entry.get("reply_to"):
            open_claims = [
                c
                for c in open_claims
                if (c["author"], c["id"]) != (entry["author"], entry["reply_to"])
            ]
    return count


def repeated_fails(entries: list[dict[str, Any]]) -> int:
    """FAIL entries whose text an earlier FAIL by a different author already recorded."""
    seen: dict[str, set[str]] = {}
    count = 0
    for entry in entries:
        if entry["kind"] != "FAIL":
            continue
        authors = seen.setdefault(" ".join(entry["text"].casefold().split()), set())
        if authors - {entry["author"]}:
            count += 1
        authors.add(entry["author"])
    return count


def idle_gaps(entries: list[dict[str, Any]], until: datetime) -> dict[str, Any]:
    closed: list[float] = []
    per_member: dict[str, list[float]] = {}
    open_gaps: dict[str, float] = {}
    members = sorted({e["author"] for e in entries if e["author"] != arcs.OWNER})
    for member in members:
        mine = [e for e in entries if e["author"] == member]
        for i, entry in enumerate(mine):
            if entry["kind"] not in ("DONE", "FAIL"):
                continue
            nxt = next((e for e in mine[i + 1 :] if e["kind"] == "CLAIM"), None)
            if nxt is not None:
                gap = _minutes((_ts(nxt["ts"]) - _ts(entry["ts"])).total_seconds())
                per_member.setdefault(member, []).append(gap)
                closed.append(gap)
            else:
                gap = _minutes((until - _ts(entry["ts"])).total_seconds())
                open_gaps[member] = max(open_gaps.get(member, 0.0), gap)
    return {
        "overall": _median_max(closed),
        "per_member": {m: _median_max(v) for m, v in per_member.items()},
        "open_gaps_min": open_gaps,
    }


def ask_ages(arc_id: str, entries: list[dict[str, Any]], until: datetime) -> dict[str, Any]:
    first_reply: dict[str, datetime] = {}
    for entry in entries:
        if entry.get("reply_to"):
            first_reply.setdefault(entry["reply_to"], _ts(entry["ts"]))
    asks = [e for e in entries if e["kind"] == "ASK"]
    replied = [
        _minutes((first_reply[a["id"]] - _ts(a["ts"])).total_seconds())
        for a in asks
        if a["id"] in first_reply
    ]
    window_ids = {a["id"] for a in asks}
    open_ids = {a["id"]: a for a in arcs.open_asks(arc_id) if a["id"] in window_ids}
    open_ages = [_minutes((until - _ts(a["ts"])).total_seconds()) for a in open_ids.values()]
    return {
        "time_to_reply_min": _median_max(replied),
        "open_asks": len(open_ids),
        "oldest_open_ask_min": max(open_ages) if open_ages else None,
    }


def reading_load(entries: list[dict[str, Any]]) -> int:
    return sum(
        len(e["text"].split())
        for e in entries
        if e["kind"] == "ASK" and e.get("to") == arcs.OWNER and e["author"] != arcs.OWNER
    )


def gh_json(args: list[str]) -> Any:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def gh_text(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def pr_view(number: int, repo: str) -> dict[str, Any]:
    fields = "number,additions,deletions,files,mergedAt,reviews,baseRefOid"
    return gh_json(["pr", "view", str(number), "-R", repo, "--json", fields])


def pr_diff(number: int, repo: str) -> str:
    return gh_text(["pr", "diff", str(number), "-R", repo])


def verified_prs(entries: list[dict[str, Any]], repo: str) -> list[dict[str, Any]]:
    numbers: list[int] = []
    for entry in entries:
        match = _PULL_RE.search(entry.get("link") or "") if entry["kind"] == "DONE" else None
        if match and int(match.group(1)) not in numbers:
            numbers.append(int(match.group(1)))
    verified = []
    for number in numbers:
        pr = pr_view(number, repo)
        approved = any(r.get("state") == "APPROVED" for r in pr.get("reviews") or [])
        if pr.get("mergedAt") and approved:
            verified.append(pr)
    return verified


def _norm_line(line: str) -> str:
    return " ".join(line.split())


def _block_hashes(lines: list[str]) -> list[str]:
    norm = [n for n in (_norm_line(x) for x in lines) if n]
    return [
        hashlib.sha1("\n".join(norm[i : i + BLOCK]).encode()).hexdigest()
        for i in range(len(norm) - BLOCK + 1)
    ]


def added_runs(diff: str) -> tuple[list[list[str]], set[str]]:
    """Runs of consecutive added lines per file in a unified diff, and the extensions touched."""
    runs: list[list[str]] = []
    exts: set[str] = set()
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            exts.add(Path(line[4:].strip()).suffix)
            current = []
        elif line.startswith("+"):
            current.append(line[1:])
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs, exts


def git_ls_tree(base_dir: Path, oid: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(base_dir), "ls-tree", "-r", "--name-only", oid],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


def git_show(base_dir: Path, oid: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(base_dir), "show", f"{oid}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def base_hashes(base_dir: Path, oid: str, exts: set[str]) -> set[str]:
    hashes: set[str] = set()
    for path in git_ls_tree(base_dir, oid):
        if Path(path).suffix not in exts:
            continue
        try:
            hashes.update(_block_hashes(git_show(base_dir, oid, path).splitlines()))
        except (UnicodeDecodeError, subprocess.CalledProcessError):
            continue
    return hashes


def duplicated_blocks(diff: str, base_dir: Path | None, base_oid: str | None) -> int | str:
    if base_dir is None or not base_oid:
        return NA
    runs, exts = added_runs(diff)
    try:
        known = base_hashes(base_dir, base_oid, exts)
    except (subprocess.CalledProcessError, OSError):
        print(
            f"base commit {base_oid} not available in {base_dir}; duplication n/a", file=sys.stderr
        )
        return NA
    return sum(1 for run in runs for h in _block_hashes(run) if h in known)


def pr_guards(pr: dict[str, Any], repo: str, base_dir: Path | None) -> dict[str, Any]:
    files = pr.get("files") or []
    return {
        "number": pr["number"],
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "new_files": sum(1 for f in files if f.get("changeType") == "ADDED"),
        "duplicated_blocks": duplicated_blocks(
            pr_diff(pr["number"], repo), base_dir, pr.get("baseRefOid")
        ),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {"pilot": {}, "baseline": {}}
    if args.arc:
        since, until = _ts(args.since), _ts(args.until)
        entries = window_entries(args.arc, since, until)
        prs = verified_prs(entries, args.repo)
        pilot_prs = [pr_view(n, args.repo) for n in args.prs] if args.prs else prs
        report["pilot"] = {
            "verified_outcomes": [p["number"] for p in prs],
            "idle_gaps": idle_gaps(entries, until),
            "ask_age": ask_ages(args.arc, entries, until),
            "collisions": collision_count(entries),
            "repeated_fails": repeated_fails(entries),
            "reading_load_words": reading_load(entries),
            "prs": [pr_guards(p, args.repo, args.base_dir) for p in pilot_prs],
        }
    if args.baseline_prs:
        report["baseline"] = {
            "verified_outcomes": NA,
            "idle_gaps": NA,
            "ask_age": NA,
            "collisions": NA,
            "repeated_fails": NA,
            "reading_load_words": NA,
            "prs": [
                pr_guards(pr_view(n, args.repo), args.repo, args.base_dir)
                for n in args.baseline_prs
            ],
        }
    return report


def _sum(prs: list[dict[str, Any]], key: str) -> Any:
    values = [p[key] for p in prs]
    return NA if any(v == NA for v in values) else sum(values)


def _row_values(side: dict[str, Any]) -> dict[str, Any]:
    if not side:
        return {}
    prs = side["prs"]
    outcomes = side["verified_outcomes"]
    gaps, asks = side["idle_gaps"], side["ask_age"]
    return {
        "Verified outcomes": len(outcomes) if outcomes != NA else NA,
        "Idle gap median/max (min)": _fmt_pair(gaps["overall"]) if gaps != NA else NA,
        "Open idle gaps": len(gaps["open_gaps_min"]) if gaps != NA else NA,
        "Ask reply median/max (min)": _fmt_pair(asks["time_to_reply_min"]) if asks != NA else NA,
        "Open asks": asks["open_asks"] if asks != NA else NA,
        "Claim collisions": side["collisions"],
        "Repeated FAILs": side["repeated_fails"],
        "Owner reading load (words)": side["reading_load_words"],
        "PR additions": _sum(prs, "additions"),
        "PR deletions": _sum(prs, "deletions"),
        "PR new files": _sum(prs, "new_files"),
        "PR duplicated blocks": _sum(prs, "duplicated_blocks"),
    }


def _fmt_pair(pair: dict[str, Any]) -> str:
    return NA if pair["median"] is None else f"{pair['median']} / {pair['max']}"


def render_markdown(report: dict[str, Any]) -> str:
    cols = {"Pilot": _row_values(report["pilot"]), "Baseline": _row_values(report["baseline"])}
    cols = {name: vals for name, vals in cols.items() if vals}
    labels = list(next(iter(cols.values()))) if cols else []
    lines = ["| Metric | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for label in labels:
        lines.append(f"| {label} | " + " | ".join(str(v[label]) for v in cols.values()) + " |")
    return "\n".join(lines)


def _numbers(value: str) -> list[int]:
    return [int(x) for x in value.split(",") if x.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--arc")
    p.add_argument("--since", type=_aware)
    p.add_argument("--until", type=_aware)
    p.add_argument("--prs", type=_numbers, default=[])
    p.add_argument("--baseline-prs", type=_numbers, default=[])
    p.add_argument("--repo", default="Atlogit/Atlomy-Atlas")
    p.add_argument("--base-dir", type=Path, help="git checkout of the repo")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.arc and not (args.since and args.until):
        p.error("--arc needs --since and --until")
    if not args.arc and not args.baseline_prs:
        p.error("give --arc or --baseline-prs")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    print(json.dumps(report, indent=2) if args.json else render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
