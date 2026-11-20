"""Quota fall-through (fork, operator ruling 2026-11-20): detection, out-marking,
routed vs pinned endings, the handoff brief and the give-up path.

Evidence the fixtures quote: 2026-11-20 runs 8b4b729acb400ca2 / d55803ed816c3c26
(Antigravity, 11:39 IST) and the gateway journal's Codex ``usageLimitExceeded``
(09:24 IST).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew import vendor_limits as vl
from kiro_crew.decisions.points import spawn_route as sr
from kiro_crew.subagent_manager import quota_switch as qs
from kiro_crew.subagent_parts.run_record import SubagentInfo

NOW = 1_795_170_000.0
AGY_FAULT = (
    "Antigravity provider fault: status ERROR: Individual quota reached. Please upgrade "
    "your subscription to increase your limits. Resets in 2h23m58s.."
)
CODEX_FAULT = (
    "{'message': 'You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to "
    "purchase more credits or try again at Nov 21st, 2026 12:00 AM.', "
    "'codexErrorInfo': 'usageLimitExceeded'}"
)
CLAUDE_FAULT = "AcpError: {'errorKind': 'rate_limit', 'message': 'Rate limited'}"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    vl._reset_for_tests()
    monkeypatch.setattr(sr, "_limits_cache", None)
    monkeypatch.setattr(sr, "_health_cache", None)
    yield
    vl._reset_for_tests()


def _agy_groups(tmp_path):
    (tmp_path / vl.ANTIGRAVITY_QUOTA_NAME).write_text(
        json.dumps(
            {
                "as_of": NOW,
                "models": {
                    "gemini-4-hydrogen-high": {"remaining_fraction": 0.2, "group": "hydrogen"},
                    "gemini-4-hydrogen-medium": {"remaining_fraction": 0.2, "group": "hydrogen"},
                    "gemini-4-neon-high": {"remaining_fraction": 0.9, "group": "neon"},
                },
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Detection, per vendor
# ---------------------------------------------------------------------------


class TestDetect:
    def test_antigravity_individual_quota_with_its_reset(self):
        hit = qs.detect(
            "failed (refusal)\n" + AGY_FAULT,
            vendor="antigravity",
            model="gemini-4-hydrogen-medium",
            now=NOW,
        )
        assert hit is not None
        assert (hit.vendor, hit.model) == ("antigravity", "gemini-4-hydrogen-medium")
        assert hit.until == NOW + 2 * 3600 + 23 * 60 + 58
        assert "Individual quota reached" in hit.detail

    @pytest.mark.parametrize("text", ["status QUOTA_EXHAUSTED", "429 RESOURCE_EXHAUSTED"])
    def test_antigravity_status_codes(self, text):
        hit = qs.detect(text, vendor="antigravity", now=NOW)
        assert hit is not None and hit.until == NOW + qs.DEFAULT_WINDOW_S

    def test_codex_usage_limit_names_its_reset(self):
        hit = qs.detect(CODEX_FAULT, vendor="codex", model="gpt-6.2", now=NOW)
        assert hit is not None
        assert hit.until == dt.datetime(2026, 11, 21, 0, 0).astimezone().timestamp()

    def test_claude_rate_limit_error(self):
        hit = qs.detect(CLAUDE_FAULT, vendor="claude", now=NOW)
        assert hit is not None and hit.until == NOW + qs.DEFAULT_WINDOW_S

    def test_claude_usage_limit_epoch(self):
        hit = qs.detect("Claude AI usage limit reached|1795180000", vendor="claude", now=NOW)
        assert hit is not None and hit.until == 1795180000.0

    def test_claude_rejected_rate_limit_event_without_error_text(self):
        vl.note_claude_rate_limit(
            {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": NOW + 600}, now=NOW
        )
        hit = qs.detect("failed (refusal)", vendor="claude", now=NOW)
        assert hit is not None and hit.until == NOW + 600
        assert hit.detail == "rate_limit_event status rejected"

    def test_another_vendors_signal_is_not_this_vendors_quota(self):
        assert qs.detect(CODEX_FAULT, vendor="antigravity", now=NOW) is None
        assert qs.detect(AGY_FAULT, vendor="claude", now=NOW) is None

    def test_an_ordinary_failure_is_not_a_quota(self):
        assert (
            qs.detect(
                "Antigravity provider fault: status ERROR: boom", vendor="antigravity", now=NOW
            )
            is None
        )
        assert qs.detect("throttling requests", vendor="claude", now=NOW) is None


class TestRunVendor:
    def test_execution_seat_beats_the_template_seat(self):
        # 2026-11-20 run 521d4974a303c81e: crew-claude template on the Antigravity seat.
        info = SimpleNamespace(agent="crew-claude", runtime_agent="crew-antigravity", backend=None)
        assert qs.run_vendor(info, "gemini-4-hydrogen-medium") == "antigravity"

    def test_model_then_seat(self):
        info = SimpleNamespace(agent="crew-claude", runtime_agent="", backend=None)
        assert qs.run_vendor(info, "gpt-6.2") == "codex"
        assert qs.run_vendor(info, "") == "claude"


# ---------------------------------------------------------------------------
# Out-marking
# ---------------------------------------------------------------------------


class TestOutMarks:
    def test_antigravity_marks_the_model_group_only(self, tmp_path):
        _agy_groups(tmp_path)
        vl.note_quota_out("antigravity", until=NOW + 100, model="gemini-4-hydrogen-high", now=NOW)
        assert vl.quota_out("antigravity", "gemini-4-hydrogen-high", now=NOW)
        assert vl.quota_out("antigravity", "gemini-4-hydrogen-medium", now=NOW)
        assert vl.quota_out("antigravity", "gemini-4-neon-high", now=NOW) is None
        assert vl.quota_outs(now=NOW)["antigravity"] == {
            "models": {"gemini-4-hydrogen-high": NOW + 100, "gemini-4-hydrogen-medium": NOW + 100}
        }

    def test_other_vendors_are_out_whole_and_it_survives_a_restart(self):
        vl.note_quota_out("codex", until=NOW + 100, model="gpt-6.2", now=NOW)
        vl._reset_for_tests()
        assert vl.quota_out("codex", "anything", now=NOW)["until"] == NOW + 100
        assert vl.quota_outs(now=NOW)["codex"] == {"until": NOW + 100}

    def test_a_mark_expires_at_its_reset_and_never_shortens(self):
        vl.note_quota_out("codex", until=NOW + 100, now=NOW)
        vl.note_quota_out("codex", until=NOW + 50, now=NOW)
        assert vl.quota_out("codex", now=NOW + 99)
        assert vl.quota_out("codex", now=NOW + 101) is None
        assert vl.note_quota_out("codex", until=NOW - 1, now=NOW) == {}

    def test_spawn_route_drops_marked_candidates(self):
        cands = [
            {
                "key": "antigravity/small/0",
                "vendor": "antigravity",
                "tier": "small",
                "model": "gemini-4-hydrogen-medium",
                "backend": "",
            },
            {
                "key": "antigravity/large/0",
                "vendor": "antigravity",
                "tier": "large",
                "model": "gemini-4-neon-high",
                "backend": "",
            },
            {
                "key": "codex/large/0",
                "vendor": "codex",
                "tier": "large",
                "model": "gpt",
                "backend": "codex",
            },
            {
                "key": "claude/small/0",
                "vendor": "claude",
                "tier": "small",
                "model": "sonnet",
                "backend": "claude",
            },
        ]
        outs = {
            "antigravity": {"models": {"gemini-4-hydrogen-medium": NOW + 1}},
            "codex": {"until": NOW + 1},
        }
        kept = sr.drop_quota_outs(cands, outs, exclude_vendors=("claude",))
        assert [c["key"] for c in kept] == ["antigravity/large/0"]

    def test_known_out_fails_a_start_on_the_marked_group(self, tmp_path):
        _agy_groups(tmp_path)
        vl.note_quota_out("antigravity", until=NOW + 100, model="gemini-4-hydrogen-high", now=NOW)
        info = SimpleNamespace(
            agent="crew-antigravity",
            runtime_agent="",
            backend=None,
            resolved_model="",
            requested_model="",
            model="",
        )
        hit = qs.known_out(info, "gemini-4-hydrogen-medium", now=NOW)
        assert hit is not None and hit.until == NOW + 100
        assert qs.known_out(info, "gemini-4-neon-high", now=NOW) is None


# ---------------------------------------------------------------------------
# Re-routing
# ---------------------------------------------------------------------------


def _tiers(vendor, config=None):
    return {
        "claude": {"top": [], "large": ["opus"], "small": ["sonnet"], "mini": []},
        "codex": {"top": [], "large": ["gpt-6.2-sol"], "small": [], "mini": []},
        "antigravity": {
            "top": [],
            "large": ["gemini-4-neon-high"],
            "small": ["gemini-4-hydrogen-medium"],
            "mini": [],
        },
    }.get(vendor, {})


BACKENDS = {
    "claude": {"backend": "claude-max", "runtime_agent": "crew-claude", "native": ""},
    "codex": {"backend": "codex", "runtime_agent": "", "native": "codex"},
}


@pytest.fixture
def no_oracle(monkeypatch):
    async def unanswered(*a, **k):
        return None

    monkeypatch.setattr(sr, "routed_spawn", unanswered)
    monkeypatch.setattr(sr, "effective_tiers", _tiers)
    monkeypatch.setattr(sr, "route_backends", lambda parent_vendor, cfg=None: dict(BACKENDS))


class TestReroute:
    def test_unanswered_oracle_falls_back_to_the_same_tier_elsewhere(self, no_oracle):
        got = asyncio.run(
            sr.reroute_after_quota(
                "t", agent="crew-antigravity", exclude_vendors=("antigravity",), tier="small"
            )
        )
        assert (got["vendor"], got["model"], got["runtime_agent"]) == (
            "claude",
            "sonnet",
            "crew-claude",
        )

    def test_marked_vendors_are_skipped(self, no_oracle):
        vl.note_quota_out("claude", until=9e12)
        got = asyncio.run(
            sr.reroute_after_quota(
                "t", agent="crew-antigravity", exclude_vendors=("antigravity",), tier="small"
            )
        )
        assert (got["vendor"], got["native_backend"]) == ("codex", "codex")

    def test_every_vendor_out_is_none(self, no_oracle):
        got = asyncio.run(
            sr.reroute_after_quota(
                "t", agent="crew-antigravity", exclude_vendors=("antigravity", "claude", "codex")
            )
        )
        assert got is None


# ---------------------------------------------------------------------------
# The ending: routed, pinned, give-up
# ---------------------------------------------------------------------------


class _Exec:
    app = ""
    memory_mode = "persistent"

    def to_record(self):
        return {"template_id": "crew-antigravity"}


class _Manager:
    _shutting_down = False

    def __init__(self, successor_error=""):
        self.spawned: list[tuple[str, dict]] = []
        self.settled: list[str | None] = []
        self._successor_error = successor_error
        self._sessions = SimpleNamespace(get_provider=lambda key: None)

    def claim_retry(self, failed):
        return ""

    def settle_retry(self, failed, successor_id):
        self.settled.append(successor_id)

    async def spawn_async(self, task, **kw):
        self.spawned.append((task, kw))
        info = SubagentInfo(id="succ0001", task=task)
        if self._successor_error:
            info.done, info.error = True, self._successor_error
        return info


def _failed(cwd="", **kw):
    info = SubagentInfo(id="fail0001", task="Review PR #5654 read-only.")
    info.agent = "crew-antigravity"
    info.requested_model = "gemini-4-hydrogen-high"
    info.cwd = cwd
    info.done = True
    info.error = "failed (refusal) — partial result preserved"
    info.result = "Read the diff; ke_components.css matches.\n" + AGY_FAULT
    info.execution_context = _Exec()
    info.quota_routable = True
    info.route_tier = "small"
    for k, v in kw.items():
        setattr(info, k, v)
    return info


ROUTED = {
    "route_id": "r",
    "model": "sonnet",
    "tier": "small",
    "vendor": "claude",
    "backend": "claude-max",
    "runtime_agent": "crew-claude",
    "native_backend": "",
    "p": 0.8,
    "reason": "",
    "vendor_out": False,
    "vendor_until": None,
    "limit": "",
    "low": [],
    "out": ["antigravity"],
    "latency_ms": 1,
}


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()

    def run(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    run("init", "-q", "-b", "fix/5654")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    (repo / "half.txt").write_text("x")
    return str(repo)


class TestRoutedFallThrough:
    def test_switches_with_a_handoff_and_records_it(self, monkeypatch, git_repo):
        asked = {}

        async def reroute(brief, **kw):
            asked.update(kw, brief=brief)
            return dict(ROUTED)

        monkeypatch.setattr(sr, "reroute_after_quota", reroute)
        mgr, info = _Manager(), _failed(cwd=git_repo)
        hit = asyncio.run(qs.handle_run_end(mgr, info, now=NOW))
        assert hit is not None and hit.vendor == "antigravity"
        # marked out at once
        assert vl.quota_out("antigravity", "gemini-4-hydrogen-high", now=NOW)
        # re-routed with the vendor excluded, at the run's tier
        assert asked["exclude_vendors"] == ("antigravity",) and asked["tier"] == "small"
        assert asked["brief"] == "Review PR #5654 read-only."
        # same task, cwd and memory identity, on the routed seat
        ((brief, kw),) = mgr.spawned
        assert kw["cwd"] == git_repo and kw["runtime_agent"] == "crew-claude"
        assert kw["model"] == "sonnet" and kw["backend"] is None
        assert kw["_execution_context"] == {"template_id": "crew-antigravity"}
        assert kw["agent"] == "crew-antigravity"
        assert brief.startswith("Review PR #5654 read-only.\n\n---\n## Handoff")
        assert "branch: fix/5654" in brief and "?? half.txt" in brief
        assert "ke_components.css matches" in brief
        # the card line and the parent's message
        until = dt.datetime.fromtimestamp(NOW + 8638).astimezone().strftime("%H:%M")
        switch = f"quota fall-through: antigravity gemini-4-hydrogen-high out until {until} -> claude sonnet"
        assert info.error.startswith(f"{switch}: continuing as run succ0001")
        assert info.error_code == "quota_exhausted"
        assert mgr.settled == ["succ0001"]

    def test_successor_carries_the_chain_and_origin(self, monkeypatch):
        captured = {}

        async def reroute(brief, **kw):
            captured.update(kw)
            return dict(ROUTED)

        monkeypatch.setattr(sr, "reroute_after_quota", reroute)

        class M(_Manager):
            async def spawn_async(self, task, **kw):
                s = await super().spawn_async(task, **kw)
                captured["succ"] = s
                return s

        info = _failed(quota_chain=("codex",), quota_origin_task="ORIGINAL")
        info.agent, info.runtime_agent = "crew-claude", "crew-antigravity"
        asyncio.run(qs.handle_run_end(M(), info, now=NOW))
        assert captured["exclude_vendors"] == ("codex", "antigravity")
        succ = captured["succ"]
        assert succ.quota_chain == ("codex", "antigravity")
        assert succ.quota_origin_task == "ORIGINAL" and succ.quota_routable is True
        assert succ.route_reason.startswith("quota fall-through: antigravity ")

    def test_gives_up_when_every_vendor_is_out(self, monkeypatch):
        async def reroute(brief, **kw):
            return None

        monkeypatch.setattr(sr, "reroute_after_quota", reroute)
        mgr, info = _Manager(), _failed()
        asyncio.run(qs.handle_run_end(mgr, info, now=NOW))
        assert mgr.spawned == [] and mgr.settled == [None]
        assert info.error.startswith(
            "quota_exhausted: antigravity gemini-4-hydrogen-high out until"
        )
        assert "every routed vendor is out (antigravity)" in info.error

    def test_a_successor_that_cannot_start_is_reported(self, monkeypatch):
        async def reroute(brief, **kw):
            return dict(ROUTED)

        monkeypatch.setattr(sr, "reroute_after_quota", reroute)
        mgr, info = _Manager(successor_error="capability_harness_unsupported"), _failed()
        asyncio.run(qs.handle_run_end(mgr, info, now=NOW))
        assert "could not start: capability_harness_unsupported" in info.error
        assert mgr.settled == [None]


class TestPinned:
    def test_pinned_run_ends_quota_exhausted_and_is_not_switched(self, monkeypatch):
        async def reroute(brief, **kw):  # pragma: no cover - must not be called
            raise AssertionError("a pinned run was re-routed")

        monkeypatch.setattr(sr, "reroute_after_quota", reroute)
        mgr, info = _Manager(), _failed(quota_routable=False)
        asyncio.run(qs.handle_run_end(mgr, info, now=NOW))
        until = dt.datetime.fromtimestamp(NOW + 8638).astimezone().strftime("%H:%M")
        assert info.error.startswith(
            f"quota_exhausted: antigravity gemini-4-hydrogen-high out until {until}. "
            "The run was pinned"
        )
        assert info.error_code == "quota_exhausted" and mgr.spawned == []
        # still marked out for every new spawn
        assert vl.quota_out("antigravity", "gemini-4-hydrogen-high", now=NOW)

    def test_undecided_reads_as_pinned(self):
        mgr, info = _Manager(), _failed(quota_routable=None)
        asyncio.run(qs.handle_run_end(mgr, info, now=NOW))
        assert mgr.spawned == [] and "pinned" in info.error


class TestNotAQuotaEnding:
    @pytest.mark.parametrize(
        "change",
        [{"user_stopped": True}, {"reaped": True}, {"error": ""}, {"_retried_as": "x"}],
    )
    def test_left_alone(self, change):
        mgr, info = _Manager(), _failed()
        for k, v in change.items():
            setattr(info, k, v)
        before = info.error
        assert asyncio.run(qs.handle_run_end(mgr, info, now=NOW)) is None
        assert info.error == before and mgr.spawned == []
        assert vl.quota_outs() == {}

    def test_ordinary_failure_marks_nothing(self):
        mgr, info = _Manager(), _failed()
        info.result = "Antigravity provider fault: status ERROR: boom."
        assert asyncio.run(qs.handle_run_end(mgr, info, now=NOW)) is None
        assert vl.quota_outs() == {}

    def test_known_out_start_keeps_its_mark(self):
        vl.note_quota_out("codex", until=NOW + 500, now=NOW)
        mgr, info = _Manager(), _failed(quota_routable=False)
        info.agent, info.requested_model = "crew-codex", "gpt-6.2"
        info.result = ""
        info.error = (
            "RuntimeError: quota_exhausted: codex gpt-6.2 out until x (known out before this start)"
        )
        hit = asyncio.run(qs.handle_run_end(mgr, info, now=NOW))
        assert hit is not None and hit.until == NOW + 500
