"""Record an arc's merged PRs on its board from GitHub.

A PR counts toward an arc when its body carries the line ``Arc: <arc_id>``
(:func:`arcs.arc_tag`); the coordinator checks the tag at the merge gate. Each
merged, tagged PR not yet on the board is recorded once as a DONE authored by
:data:`arcs.GITHUB`, with its head, merge commit and the reviewers whose clean
review covers that exact head.

A review is a PR comment carrying a ``ke-review`` marker::

    <!-- ke-review head=<sha> route=session reviewer=assay ... verdict=clean -->

A PR is *verified* when at least one reviewer in :data:`REVIEWERS` left a clean
marker whose ``head`` is the PR's final head (a short sha matches as a prefix).

Reads GitHub through the ``gh`` CLI and its own login; nothing here writes to
GitHub.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable

from kiro_crew import arcs

#: Reviewer lanes whose clean review verifies a PR (``reviewer=`` in the marker).
REVIEWERS = frozenset({"assay", "derrick"})

_MARKER_RE = re.compile(r"<!--\s*ke-review\b([^>]*)-->")
_ATTR_RE = re.compile(r"([a-z_]+)=([^\s]+)")

GhRunner = Callable[[list[str]], Any]


def _gh_json(args: list[str]) -> Any:
    out = subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    return json.loads(out.stdout or "null")


def review_markers(comments: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Every ``ke-review`` marker in the comments, as attribute dicts."""
    found = []
    for comment in comments:
        for match in _MARKER_RE.finditer(str(comment.get("body") or "")):
            found.append(dict(_ATTR_RE.findall(match.group(1))))
    return found


def clean_reviewers(comments: list[dict[str, Any]], head: str) -> list[str]:
    """Reviewers in :data:`REVIEWERS` with a clean review of exactly ``head``."""
    if not head:
        return []
    who: list[str] = []
    for marker in review_markers(comments):
        reviewer = marker.get("reviewer", "").lower()
        reviewed = marker.get("head", "")
        if (
            reviewer in REVIEWERS
            and marker.get("verdict") == "clean"
            and len(reviewed) >= 7
            and head.startswith(reviewed)
            and reviewer not in who
        ):
            who.append(reviewer)
    return who


def lane_for_branch(arc: dict[str, Any], branch: str) -> str:
    """The member whose role names the branch prefix (``tenon/...`` -> Tenon)."""
    prefix = branch.split("/", 1)[0].casefold() if "/" in branch else ""
    if not prefix:
        return ""
    matches = [
        key
        for key, member in arc["members"].items()
        if (member.get("role") or "").casefold().split(" ")[0] == prefix
    ]
    return matches[0] if len(matches) == 1 else ""


def tagged(body: str, arc_id: str) -> bool:
    """Whether the PR body carries the arc's tag on a line of its own."""
    tag = arcs.arc_tag(arc_id)
    return any(line.strip().strip("*_`").strip() == tag for line in str(body or "").splitlines())


def sync_arc(arc_id: str, *, gh: GhRunner = _gh_json, limit: int = 50) -> list[dict[str, Any]]:
    """Record every merged, tagged PR not yet on the board; return the new entries."""
    arc = arcs.read_arc(arc_id)
    repo = arc.get("repo") or ""
    if not repo:
        return []
    listed = gh(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--search",
            f'"{arcs.arc_tag(arc_id)}" in:body',
            "--limit",
            str(limit),
            "--json",
            "number,title,url,body,headRefName,mergedAt",
        ]
    )
    known = {e.get("pr") for e in arcs.read_entries(arc_id, limit=10**9) if e.get("pr")}
    recorded = []
    for pr in sorted(listed or [], key=lambda p: p.get("mergedAt") or ""):
        number = pr.get("number")
        if number in known or not tagged(pr.get("body") or "", arc_id):
            continue
        detail = gh(
            [
                "pr",
                "view",
                str(number),
                "-R",
                repo,
                "--json",
                "headRefOid,mergeCommit,comments",
            ]
        )
        head = str(detail.get("headRefOid") or "")
        entry = arcs.record_merged_pr(
            arc_id,
            {
                "number": number,
                "title": pr.get("title") or "",
                "url": pr.get("url") or "",
                "head": head,
                "merge_sha": str((detail.get("mergeCommit") or {}).get("oid") or ""),
                "merged_at": pr.get("mergedAt") or "",
                "reviewers": clean_reviewers(detail.get("comments") or [], head),
                "lane": lane_for_branch(arc, pr.get("headRefName") or ""),
            },
        )
        if entry is not None:
            recorded.append(entry)
    return recorded


def sync_all(*, gh: GhRunner = _gh_json) -> dict[str, int]:
    """Sync every arc that names a repo; one arc's failure does not stop the rest."""
    counts: dict[str, int] = {}
    for arc in arcs.list_arcs():
        if not arc.get("repo"):
            continue
        try:
            counts[arc["id"]] = len(sync_arc(arc["id"], gh=gh))
        except (arcs.ArcError, OSError, subprocess.SubprocessError, ValueError):
            counts[arc["id"]] = -1
    return counts
