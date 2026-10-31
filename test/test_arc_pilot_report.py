"""Pilot report: board-derived coordination metrics and PR product guards."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew import arcs

_SPEC = importlib.util.spec_from_file_location(
    "arc_pilot_report", Path(__file__).resolve().parents[1] / "scripts" / "arc_pilot_report.py"
)
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


class Board:
    def __init__(self, monkeypatch, *members):
        self.clock = T0
        monkeypatch.setattr(arcs, "_now", lambda: self.clock)
        arc = arcs.create_arc(
            {
                "title": "Pilot",
                "goal": "g",
                "reason": "r",
                "lineage": "",
                "source": "t",
                "autonomy": 2,
                "milestones": [{"name": "m0", "criteria": "c0"}],
                "decisions": ["ADR-1"],
                "rules": [],
                "reuse": [],
                "channel_id": "chan",
            }
        )
        self.id = arc["id"]
        for m in members:
            arcs.add_member(self.id, m, "worker")

    def add(self, minutes, author, kind, text="t", **extra):
        self.clock = T0 + timedelta(minutes=minutes)
        if kind == "CLAIM":
            extra.setdefault("step", "s")
            extra.setdefault("checked", ["ADR-1"])
        if kind == "DONE":
            extra.setdefault("link", "l")
        return arcs.append_entry(self.id, author, {"kind": kind, "text": text, **extra})


def _window(board, hours=10):
    return report.window_entries(board.id, T0, T0 + timedelta(hours=hours)), T0 + timedelta(
        hours=hours
    )


def test_idle_gap_from_done_to_next_claim(monkeypatch):
    b = Board(monkeypatch, "a", "b")
    b.add(0, "a", "DONE")
    b.add(10, "a", "CLAIM")
    b.add(20, "a", "FAIL")
    b.add(60, "a", "CLAIM")
    b.add(30, "b", "DONE")
    entries, until = _window(b)
    gaps = report.idle_gaps(entries, until)
    assert gaps["per_member"]["a"] == {"median": 25.0, "max": 40.0}
    assert gaps["overall"]["max"] == 40.0
    assert gaps["open_gaps_min"] == {"b": 570.0}


def test_ask_age_median_and_open(monkeypatch):
    b = Board(monkeypatch, "a")
    first = b.add(0, "a", "ASK", to="owner")
    second = b.add(1, "a", "ASK", to="owner")
    b.add(10, "owner", "FACT", reply_to=first["id"])
    b.add(5, "a", "ASK", to="owner", text="one two three")
    b.add(30, "owner", "FACT", reply_to=second["id"])
    entries, until = _window(b)
    ages = report.ask_ages(b.id, entries, until)
    assert ages["time_to_reply_min"] == {"median": 19.5, "max": 29.0}
    assert ages["open_asks"] == 1
    assert report.reading_load(entries) == 5


def test_collisions_and_repeated_fails(monkeypatch):
    b = Board(monkeypatch, "a", "b")
    mine = b.add(0, "a", "CLAIM")
    b.add(1, "b", "CLAIM")
    b.add(2, "a", "DONE", reply_to=mine["id"])
    b.add(3, "b", "CLAIM", step="other")
    b.add(4, "a", "FAIL", text="Bad  thing")
    b.add(5, "b", "FAIL", text="bad thing")
    entries, _ = _window(b)
    assert report.collision_count(entries) == 1
    assert report.repeated_fails(entries) == 1


def test_window_is_half_open(monkeypatch):
    b = Board(monkeypatch, "a")
    b.add(0, "a", "FACT")
    b.add(60, "a", "FACT")
    got = report.window_entries(b.id, T0, T0 + timedelta(minutes=60))
    assert len(got) == 1


def test_duplicate_block_detected(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    block = ["a = 1", "b = 2", "c = 3", "d = 4", "e = 5"]
    (base / "m.py").write_text("\n\n".join(["x", *block, "y"]) + "\n")
    diff = (
        "+++ b/new.py\n"
        + "".join(f"+  {line}\n" for line in block[:3])
        + "+\n"
        + "".join(f"+{line}\n" for line in block[3:])
        + " ctx\n+unique line\n"
    )
    assert report.duplicated_blocks(diff, base) == 1
    assert report.duplicated_blocks(diff, None) == "n/a"
    assert report.duplicated_blocks(diff.replace("e = 5", "e = 6"), base) == 0


def _pr(number, merged=True, state="APPROVED"):
    return {
        "number": number,
        "additions": 10,
        "deletions": 2,
        "files": [{"path": "a", "changeType": "ADDED"}, {"path": "b", "changeType": "MODIFIED"}],
        "mergedAt": "2026-10-02T00:00:00Z" if merged else None,
        "reviews": [{"state": state}],
    }


def test_verified_outcomes_need_merge_and_approval(monkeypatch):
    b = Board(monkeypatch, "a")
    for n in (1, 2, 3):
        b.add(n, "a", "DONE", link=f"https://github.com/o/r/pull/{n}")
    prs = {1: _pr(1), 2: _pr(2, merged=False), 3: _pr(3, state="COMMENTED")}
    monkeypatch.setattr(report, "pr_view", lambda n, repo: prs[n])
    entries, _ = _window(b)
    assert [p["number"] for p in report.verified_prs(entries, "o/r")] == [1]


def test_baseline_rows_are_na_for_board_metrics(monkeypatch, capsys):
    monkeypatch.setattr(report, "pr_view", lambda n, repo: _pr(n))
    monkeypatch.setattr(report, "pr_diff", lambda n, repo: "+++ b/x.py\n+one\n")
    assert report.main(["--baseline-prs", "7,8", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["baseline"]["collisions"] == "n/a"
    assert data["baseline"]["prs"][0]["new_files"] == 1
    assert data["baseline"]["prs"][0]["duplicated_blocks"] == "n/a"
    assert report.main(["--baseline-prs", "7"]) == 0
    out = capsys.readouterr().out
    assert "| Claim collisions | n/a |" in out
    assert "| PR additions | 10 |" in out
