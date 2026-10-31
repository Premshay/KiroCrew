"""``kirocrew arc``: the owner's side of the per-arc board.

Writes go straight to the arc store as the local owner: whoever can read and write
``KIROCREW_HOME`` is the owner, and no further authentication is added (a documented
pilot limitation). Only the answer's delivery goes through the gateway.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from kiro_crew import arc_brief, arcs
from kiro_crew.cli_commands_gateway_api import _gateway_send, _internal_secret, _peer_refusal
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.terminal_safe import safe_terminal_line

_SHOWN_ENTRIES = 20


def _fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _age(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _role(arc: dict[str, Any], key: str) -> str:
    if key == arcs.OWNER:
        return arcs.OWNER
    return str(arc["members"].get(key, {}).get("role") or key)


def _show(arc_id: str) -> str:
    arc = arcs.read_arc(arc_id)
    line = arc_brief.safe_line
    milestones = arc["milestones"]
    index = arc["milestone_index"]
    out = [f"{line(arc['title'])} ({arc['id']})"]
    if index < len(milestones):
        current = milestones[index]
        out.append(
            f"Milestone {index + 1}/{len(milestones)}: {line(current['name'])}"
            f" — {line(str(current.get('criteria') or 'no criteria'))}"
        )
    else:
        out.append(f"Milestones: all {len(milestones)} accepted")
    out.append("Open claims:")
    claims = arcs.open_claims(arc_id)
    now = datetime.now().astimezone()
    for c in claims:
        age = max(0, int((now - datetime.fromisoformat(c["ts"])).total_seconds()))
        out.append(f"  {_role(arc, c['author'])}: {line(c.get('step') or '')} ({_age(age)})")
    if not claims:
        out.append("  none")
    out.append("Open asks:")
    asks = arcs.open_asks(arc_id)
    for a in asks:
        seen = [_role(arc, k) for k in arcs.seen_by(arc_id, a["id"])]
        out.append(
            f"  {a['id']} {_role(arc, a['author'])} -> {_role(arc, a.get('to') or '')}"
            f" ({_age(a['age_secs'])}): {line(a['text'])}"
            f" | seen by: {', '.join(seen) if seen else 'none'}"
        )
    if not asks:
        out.append("  none")
    out.append(f"Last {_SHOWN_ENTRIES} entries:")
    for e in arcs.read_entries(arc_id, limit=10**9)[-_SHOWN_ENTRIES:]:
        out.append(f"  {arc_brief.entry_line(arc, e)}")
    return "\n".join(out)


def _request_delivery(arc_id: str, entry_id: str) -> str | None:
    """Ask the gateway to deliver a stored entry; the reason it could not, else None."""
    try:
        cfg = KiroCrewConfig.load()
        _host, port = parse_dashboard_url(cfg.dashboard.url)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/arcs/{arc_id}/deliver/{entry_id}",
            data=b"",
            headers={"X-Internal-Secret": _internal_secret(port)},
            method="POST",
        )
        with _gateway_send(req, port, 30, "cli_arc_deliver") as resp:
            resp.read()
    except _peer_refusal() as exc:
        return f"gateway ownership not verified: {exc}"
    except urllib.error.HTTPError as exc:
        return f"gateway refused delivery (HTTP {exc.code})"
    except Exception as exc:
        return str(exc)
    return None


def _arc(args: argparse.Namespace) -> None:
    """Create arcs, add members, show the board, answer asks, accept milestones."""
    action = getattr(args, "arc_action", None)
    try:
        if action == "create":
            spec = json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
            print(arcs.create_arc(spec)["id"])
        elif action == "add-member":
            arcs.add_member(args.arc_id, args.session_key, args.role)
        elif action == "show":
            print(_show(args.arc_id))
        elif action == "answer":
            known = {
                e["id"] for e in arcs.read_entries(args.arc_id, limit=10**9) if e["kind"] == "ASK"
            }
            if args.ask_id not in known:
                _fail(f"{args.ask_id} is not an ASK in arc {args.arc_id}")
            entry = arcs.append_entry(
                args.arc_id,
                arcs.OWNER,
                {"kind": "FACT", "text": args.text, "reply_to": args.ask_id},
            )
            problem = _request_delivery(args.arc_id, entry["id"])
            if problem:
                print(
                    f"Warning: answer stored but not delivered now ({safe_terminal_line(problem)});"
                    " the asker sees it in its next brief.",
                    file=sys.stderr,
                )
        elif action == "accept":
            arcs.accept_milestone(args.arc_id)
        else:
            print("Usage: kirocrew arc {create|add-member|show|answer|accept}", file=sys.stderr)
            sys.exit(1)
    except (arcs.ArcError, OSError, ValueError) as exc:
        _fail(str(exc))
