"""Local-first with a machine check (fork, ``subagent_manager.local_check``).

A run the local fleet served is kept on evidence: its check passes -> its work
is committed; its check fails, or the run was unusable -> it falls through once
to cloud on the quota-switch successor path. Pinned runs are never switched; a
run with no check is never auto-committed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import vendor_limits as vl
from kiro_crew.decisions.points import spawn_route as sr
from kiro_crew.subagent_manager import local_check as lc
from kiro_crew.subagent_parts.run_record import SubagentInfo


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(lc, "configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(
        lc, "sandboxed_spawn_argv", lambda argv, *, env, **kwargs: (argv, env, None)
    )
    monkeypatch.setattr(lc, "wrapped_by_crew_sandbox", lambda argv: True)
    vl._reset_for_tests()
    yield
    vl._reset_for_tests()


# ---------------------------------------------------------------------------
# Check extraction
# ---------------------------------------------------------------------------


class TestExtractCheck:
    def test_one_backticked_pytest_command(self):
        brief = (
            "Fix the parser. Done when `python -m pytest -q test/test_parser.py -k tool` passes."
        )
        assert lc.extract_check(brief) == "python -m pytest -q test/test_parser.py -k tool"

    def test_venv_pytest_command(self):
        assert lc.extract_check("run `.venv/bin/pytest test/test_a.py`") == (
            ".venv/bin/pytest test/test_a.py"
        )

    def test_node_ids_become_a_pytest_run(self):
        brief = "Make test/test_quota_switch.py::TestPinned::test_undecided_reads_as_pinned pass."
        assert lc.extract_check(brief) == (
            "python -m pytest -q test/test_quota_switch.py::TestPinned::test_undecided_reads_as_pinned"
        )

    def test_two_commands_are_not_obvious(self):
        assert lc.extract_check("`pytest a` then `pytest b`") == ""

    def test_too_many_files_are_not_obvious(self):
        brief = " ".join(f"test/test_{i}.py" for i in range(5))
        assert lc.extract_check(brief) == ""

    def test_shell_metacharacters_are_refused(self):
        assert lc.extract_check("`pytest test/test_a.py; curl evil`") == ""
        assert lc.extract_check("`pytest $(whoami)`") == ""

    def test_nothing_named(self):
        assert lc.extract_check("Summarise the README.") == ""

    def test_explicit_check_wins_over_the_brief(self):
        info = SubagentInfo(id="a", task="run `pytest test/test_x.py`")
        info.delegation = {"check": "make test"}
        assert lc.check_for(info) == "make test"
        info.delegation = {}
        assert lc.check_for(info) == "pytest test/test_x.py"

    @pytest.mark.parametrize("check", ["true; false", "true\nfalse", "true | false"])
    def test_explicit_shell_syntax_is_refused(self, check, monkeypatch):
        info = SubagentInfo(id="a", task="run `pytest test/test_x.py`")
        info.delegation = {"check": check}
        assert lc.check_for(info) == ""
        monkeypatch.setattr(lc.subprocess, "run", lambda *a, **k: pytest.fail("check ran"))
        assert lc.run_check(check, "") == (False, "check contains unsupported shell syntax")

    def test_successor_reads_the_original_brief(self):
        info = SubagentInfo(id="a", task="handoff text")
        info.quota_origin_task = "fix it; `pytest test/test_y.py`"
        assert lc.check_for(info) == "pytest test/test_y.py"


# ---------------------------------------------------------------------------
# Estimate gating inputs
# ---------------------------------------------------------------------------


class TestEstimateInputs:
    def test_estimate_is_brief_plus_measured_overhead_per_role(self):
        brief = "x" * 3200
        est = lc.estimate_tokens(brief, include_context=False)
        assert est == {
            "subagent": 1001 + lc.LOCAL_HARNESS_OVERHEAD_TOKENS["subagent"],
            "agent": 1001 + lc.LOCAL_HARNESS_OVERHEAD_TOKENS["agent"],
        }
        with_ctx = lc.estimate_tokens(brief)
        assert with_ctx["agent"] - est["agent"] == lc.INJECTED_CONTEXT_TOKENS

    def test_state_carries_has_check_and_the_full_brief_estimate(self):
        long_brief = "y" * (sr.MAX_BRIEF_CHARS * 4)
        state = sr.build_state(
            long_brief, vendor="claude", parent_model="", cands=[], health={}, has_check=True
        )
        assert state["has_check"] is True
        assert len(state["brief"]) == sr.MAX_BRIEF_CHARS
        # estimated on the whole brief, not the truncated one sent
        assert state["estimate_tokens"] == lc.estimate_tokens(long_brief)

    def test_local_health_reads_ctx_per_role_and_cpu_busy(self):
        status = {
            "lanes": {
                "nexus-swap": {"available": True, "live_ctx": {"qwen-fast": 32768}},
                "noda-swap": {"available": False, "live_ctx": {"qwen38-mtp": 131072}},
            },
            "roles": {
                "subagent": {"lane": "nexus-swap", "model": "qwen-fast"},
                "agent": {"lane": "noda-swap", "model": "qwen38-mtp"},
            },
            "host": {"cpu_busy": 0.42},
        }
        h = vl.local_health(status)
        assert h["lanes"] == {"subagent": True, "agent": False}
        assert h["ctx"] == {"subagent": 32768, "agent": 131072}
        assert h["cpu_busy"] == 0.42
        state = sr.build_state("t", vendor="claude", parent_model="", cands=[], health={"local": h})
        assert state["health"]["local"]["ctx"] == {"subagent": 32768, "agent": 131072}
        assert state["health"]["local"]["cpu_busy"] == 0.42

    def test_cpu_busy_absent_is_tolerated(self):
        status = {
            "lanes": {"nexus-swap": {"available": True}},
            "roles": {"subagent": {"lane": "nexus-swap", "model": "qwen-fast"}},
        }
        h = vl.local_health(status)
        assert "cpu_busy" not in h and "ctx" not in h
        assert h["lanes"] == {"subagent": True, "agent": False}


# ---------------------------------------------------------------------------
# The ending
# ---------------------------------------------------------------------------


class _Exec:
    app = ""
    memory_mode = "persistent"

    def to_record(self):
        return {"template_id": "kirocrew"}


class _Manager:
    _shutting_down = False

    def __init__(self):
        self.spawned: list[tuple[str, dict]] = []
        self.settled: list[str | None] = []
        self._sessions = SimpleNamespace(get_provider=lambda key: None)

    def claim_retry(self, failed):
        return ""

    def settle_retry(self, failed, successor_id):
        self.settled.append(successor_id)

    async def spawn_async(self, task, **kw):
        self.spawned.append((task, kw))
        return SubagentInfo(id="cloud001", task=task)


CLOUD = {
    "route_id": "r",
    "model": "gpt-6.2-sol",
    "tier": "small",
    "vendor": "codex",
    "backend": "codex",
    "runtime_agent": "",
    "native_backend": "codex",
    "p": None,
    "reason": "",
    "vendor_out": False,
    "vendor_until": None,
    "limit": "",
    "low": [],
    "out": [],
    "latency_ms": 1,
}


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()

    def run(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    run("init", "-q", "-b", "feat/x")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("commit", "-q", "--allow-empty", "-m", "base")
    (repo / "work.txt").write_text("done by the local worker\n")
    return str(repo)


def _local(cwd="", **kw):
    info = SubagentInfo(id="loc00001", task="Write work.txt.")
    info.agent = "kirocrew"
    info.route_vendor = "local"
    info.route_tier = "subagent"
    info.cwd = cwd
    info.done = True
    info.result = "I wrote work.txt."
    info.execution_context = _Exec()
    info.quota_routable = True
    for k, v in kw.items():
        setattr(info, k, v)
    return info


@pytest.fixture
def reroute(monkeypatch):
    asked: dict = {}

    async def fake(brief, **kw):
        asked.update(kw, brief=brief)
        return dict(CLOUD)

    monkeypatch.setattr(sr, "reroute_after_quota", fake)
    return asked


def _head(repo):
    return subprocess.run(
        ["git", "-C", repo, "log", "-1", "--format=%h %s"], capture_output=True, text=True
    ).stdout.strip()


def _capture_commit_scope(info):
    snapshot = getattr(lc, "snapshot_commit_scope", None)
    if snapshot is not None:
        info._local_commit_scope = snapshot(info.cwd)


class TestPassCommits:
    def test_pass_commits_the_workers_changes(self, git_repo, reroute):
        mgr = _Manager()
        info = _local(cwd=git_repo, delegation={"check": "test -f work.txt"})
        info._ending_claimed = True
        Path(git_repo, "work.txt").unlink()
        _capture_commit_scope(info)
        Path(git_repo, "work.txt").write_text("worker change", encoding="utf-8")
        subprocess.run(["git", "-C", git_repo, "add", "work.txt"], check=True)
        Path(git_repo, "private.txt").write_text("untracked", encoding="utf-8")
        assert asyncio.run(lc.handle_run_end(mgr, info)) == "pass"
        sha, subject = _head(git_repo).split(" ", 1)
        assert subject == "local run loc00001: check passed"
        assert info.local_check_line == f"local check: test -f work.txt PASS -> committed {sha}"
        assert not info.error and mgr.spawned == []
        status = subprocess.run(
            ["git", "-C", git_repo, "status", "--porcelain"], capture_output=True, text=True
        ).stdout
        assert status == "?? private.txt\n"

    def test_pass_on_a_protected_branch_is_not_committed(self, git_repo, reroute):
        subprocess.run(["git", "-C", git_repo, "checkout", "-q", "-b", "main"], check=True)
        info = _local(cwd=git_repo, delegation={"check": "true"})
        info._local_commit_scope = lc.snapshot_commit_scope(git_repo)
        assert asyncio.run(lc.handle_run_end(_Manager(), info)) == "pass_uncommitted"
        assert "protected branch main" in info.local_check_line
        assert _head(git_repo).endswith("base")

    def test_pass_with_nothing_to_commit(self, git_repo, reroute):
        subprocess.run(["git", "-C", git_repo, "clean", "-fdq"], check=True)
        info = _local(cwd=git_repo, delegation={"check": "true"})
        info._local_commit_scope = lc.snapshot_commit_scope(git_repo)
        assert asyncio.run(lc.handle_run_end(_Manager(), info)) == "pass_uncommitted"
        assert (
            info.local_check_line == "local check: true PASS -> not committed (no eligible changes)"
        )

    def test_dirty_primary_checkout_is_refused(self, git_repo):
        info = _local(cwd=git_repo, delegation={"check": "true"})
        _capture_commit_scope(info)
        assert asyncio.run(lc.handle_run_end(_Manager(), info)) == "pass_uncommitted"
        assert "working directory was dirty before the run" in info.local_check_line

    def test_linked_worktree_leaves_preexisting_paths_and_runs_hooks(self, git_repo, tmp_path):
        worktree = tmp_path / "linked"
        subprocess.run(
            ["git", "-C", git_repo, "worktree", "add", "-qb", "linked", str(worktree)],
            check=True,
        )
        (worktree / "user.txt").write_text("preexisting work", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "user.txt"], check=True)
        info = _local(cwd=str(worktree), delegation={"check": "true"})
        _capture_commit_scope(info)
        (worktree / "new.txt").write_text("worker change", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "new.txt"], check=True)
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\necho hook-refused >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", git_repo, "config", "core.hooksPath", str(hooks)], check=True)
        assert asyncio.run(lc.handle_run_end(_Manager(), info)) == "pass_uncommitted"
        assert "hook-refused" in info.local_check_line
        hook.unlink()
        assert asyncio.run(lc.handle_run_end(_Manager(), info)) == "pass"
        assert (
            subprocess.check_output(
                ["git", "-C", str(worktree), "show", "--pretty=", "--name-only", "HEAD"], text=True
            ).strip()
            == "new.txt"
        )
        assert (
            subprocess.check_output(
                ["git", "-C", str(worktree), "status", "--porcelain"], text=True
            )
            == "A  user.txt\n"
        )

    def test_opt_in_defaults_off(self, git_repo):
        from kiro_crew.config import KiroCrewConfig

        section = KiroCrewConfig().to_dict().get("subagent", {})
        assert section.get("local_check", {}).get("auto_commit") is False
        info = _local(cwd=git_repo, delegation={"check": "true"})
        assert asyncio.run(lc.handle_run_end(_Manager(), info)) == "pass_uncommitted"

    def test_run_stopped_during_check_does_not_commit(self, git_repo, monkeypatch):
        info = _local(cwd=git_repo, delegation={"check": "true"})
        _capture_commit_scope(info)

        def stopped(*args):
            info.user_stopped = True
            return True, ""

        monkeypatch.setattr(lc, "run_check", stopped)
        monkeypatch.setattr(lc, "commit_changes", lambda *a: pytest.fail("commit ran"))
        assert asyncio.run(lc.handle_run_end(_Manager(), info)) == ""

    @pytest.mark.asyncio
    async def test_cancelled_finalizer_stops_pending_commit(self, git_repo, monkeypatch):
        info = _local(cwd=git_repo, delegation={"check": "true"})
        Path(git_repo, "work.txt").unlink()
        _capture_commit_scope(info)
        Path(git_repo, "work.txt").write_text("worker change", encoding="utf-8")
        subprocess.run(["git", "-C", git_repo, "add", "work.txt"], check=True)
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        settled = asyncio.Event()
        original_git = lc._git

        def blocked_git(cwd, *args):
            if args[0] == "status":
                loop.call_soon_threadsafe(started.set)
                release.wait()
            return original_git(cwd, *args)

        def commit(*args):
            try:
                return original_commit(*args)
            finally:
                loop.call_soon_threadsafe(settled.set)

        original_commit = lc.commit_changes
        monkeypatch.setattr(lc, "_git", blocked_git)
        monkeypatch.setattr(lc, "commit_changes", commit)
        job = asyncio.create_task(lc.handle_run_end(_Manager(), info))
        try:
            await started.wait()
            job.cancel()
            scheduled = asyncio.Event()
            loop.call_soon(scheduled.set)
            await scheduled.wait()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        await settled.wait()
        assert _head(git_repo).endswith("base")


class TestFailFallsThrough:
    def test_failed_check_goes_to_cloud_with_a_handoff(self, git_repo, reroute, monkeypatch):
        mgr = _Manager()
        monkeypatch.setattr(lc, "run_check", lambda *a: (False, "2 failed\n(exit 1)"))
        info = _local(
            cwd=git_repo,
            delegation={"check": "pytest"},
            quota_chain=("antigravity",),
        )
        assert asyncio.run(lc.handle_run_end(mgr, info)) == lc.REASON_CHECK_FAILED
        # the local lane and every earlier vendor are excluded; a cloud tier is asked for
        assert reroute["exclude_vendors"] == ("antigravity", "local")
        assert reroute["tier"] == sr.FALLBACK_TIER
        ((brief, kw),) = mgr.spawned
        assert kw["cwd"] == git_repo and kw["model"] == "gpt-6.2-sol" and kw["backend"] == "codex"
        assert kw["_execution_context"] == {"template_id": "kirocrew"}
        assert kw["delegation"] == {"check": "pytest"}
        assert brief.startswith("Write work.txt.\n\n---\n## Handoff (local_check_failed")
        assert "2 failed" in brief and "(exit 1)" in brief
        assert "branch: feat/x" in brief and "?? work.txt" in brief
        assert "I wrote work.txt." in brief
        assert info.local_check_line == "local check FAIL -> codex gpt-6.2-sol"
        assert info.error.startswith(
            "local_check_failed: local check FAIL -> codex gpt-6.2-sol: "
            "continuing as run cloud001"
        )
        assert info.error_code == lc.REASON_CHECK_FAILED
        assert mgr.settled == ["cloud001"]
        # nothing was committed on a failed check
        assert _head(git_repo).endswith("base")

    def test_successor_is_routable_and_never_returns_to_local(self, reroute):
        captured = {}

        class M(_Manager):
            async def spawn_async(self, task, **kw):
                s = await super().spawn_async(task, **kw)
                captured["succ"] = s
                return s

        info = _local(delegation={"check": "false"})
        asyncio.run(lc.handle_run_end(M(), info))
        succ = captured["succ"]
        assert succ.quota_routable is True and succ.quota_chain == ("local",)
        assert succ.route_vendor == "codex" and succ.quota_origin_task == "Write work.txt."
        assert succ.route_reason.startswith("local_check_failed: local check FAIL -> codex")

    def test_a_cloud_successor_is_not_checked_again(self, reroute):
        succ = _local(route_vendor="codex", delegation={"check": "false"})
        succ.agent = "crew-codex"
        assert asyncio.run(lc.handle_run_end(_Manager(), succ)) == ""


class TestUnusable:
    @pytest.mark.parametrize(
        "error",
        [
            "context_window_exceeded: request (40000 tokens) exceeds the available context size",
            "lane_busy: nexus-swap has an active request",
            "Reaped after 900s (exceeded 900s deadline)",
            "RuntimeError: adapter crashed",
        ],
    )
    def test_unusable_run_goes_to_cloud_without_running_the_check(self, error, reroute):
        mgr = _Manager()
        info = _local(error=error, delegation={"check": "touch ran-the-check"})
        assert asyncio.run(lc.handle_run_end(mgr, info)) == lc.REASON_UNUSABLE
        ((brief, _),) = mgr.spawned
        assert "## Handoff (local_unusable" in brief and error[:40] in brief
        assert info.local_check_line == "local run unusable -> codex gpt-6.2-sol"
        assert info.error_code == lc.REASON_UNUSABLE

    def test_unusable_without_a_check_still_falls_through(self, reroute):
        mgr = _Manager()
        info = _local(error="context_window_exceeded")
        assert asyncio.run(lc.handle_run_end(mgr, info)) == lc.REASON_UNUSABLE
        assert len(mgr.spawned) == 1

    def test_no_cloud_left_is_reported(self, monkeypatch):
        async def none(brief, **kw):
            return None

        monkeypatch.setattr(sr, "reroute_after_quota", none)
        mgr = _Manager()
        info = _local(error="lane_busy")
        asyncio.run(lc.handle_run_end(mgr, info))
        assert mgr.spawned == [] and mgr.settled == [None]
        assert info.local_check_line == "local run unusable -> no cloud candidate left"


class TestPinned:
    def test_pinned_failed_check_is_not_switched(self, git_repo, monkeypatch):
        async def boom(brief, **kw):  # pragma: no cover - must not be called
            raise AssertionError("a pinned run was re-routed")

        monkeypatch.setattr(sr, "reroute_after_quota", boom)
        mgr = _Manager()
        info = _local(cwd=git_repo, quota_routable=False, delegation={"check": "exit 3"})
        assert asyncio.run(lc.handle_run_end(mgr, info)) == lc.REASON_CHECK_FAILED
        assert mgr.spawned == [] and mgr.settled == []
        assert info.local_check_line == "local check FAIL -> not switched (pinned run)"
        assert info.error.startswith("local_check_failed: the check `exit 3` failed")
        assert _head(git_repo).endswith("base")

    def test_pinned_unusable_keeps_its_error(self, monkeypatch):
        async def boom(brief, **kw):  # pragma: no cover
            raise AssertionError("a pinned run was re-routed")

        monkeypatch.setattr(sr, "reroute_after_quota", boom)
        info = _local(quota_routable=None, error="lane_busy")
        asyncio.run(lc.handle_run_end(_Manager(), info))
        assert info.error == "lane_busy"
        assert info.local_check_line == "local run unusable -> not switched (pinned run)"


class TestNoCheck:
    def test_no_check_means_no_auto_commit(self, git_repo, reroute):
        mgr = _Manager()
        info = _local(cwd=git_repo)
        assert asyncio.run(lc.handle_run_end(mgr, info)) == ""
        assert info.local_check_line == "" and mgr.spawned == []
        assert _head(git_repo).endswith("base")
        status = subprocess.run(
            ["git", "-C", git_repo, "status", "--porcelain"], capture_output=True, text=True
        ).stdout
        assert "work.txt" in status

    @pytest.mark.parametrize(
        "change", [{"user_stopped": True}, {"reaped": True}, {"_retried_as": "x"}]
    )
    def test_left_alone(self, change, reroute):
        mgr = _Manager()
        info = _local(delegation={"check": "false"})
        for k, v in change.items():
            setattr(info, k, v)
        assert asyncio.run(lc.handle_run_end(mgr, info)) == ""
        assert mgr.spawned == []


class TestRunCheck:
    def test_requires_sandbox_before_starting_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lc, "wrapped_by_crew_sandbox", lambda argv: False)
        monkeypatch.setattr(
            lc, "run_limited", lambda *a, **kw: pytest.fail("Started without sandbox")
        )

        passed, output = lc.run_check("true", str(tmp_path))

        assert not passed
        assert "a Kiro Crew sandbox is required" in output

    def test_launches_wrapped_command_and_releases_launcher(self, tmp_path, monkeypatch):
        launcher = tmp_path / "launcher"
        launcher.touch()
        wrapped = [str(launcher), "pytest", "test_example.py"]
        env = {"PATH": "/test/bin", "HOME": str(tmp_path), "LANG": "C"}
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("GATEWAY_TEST_SECRET", "fixture-value")

        def prepare(argv, **options):
            assert argv == ["pytest", "test_example.py"]
            assert options == {
                "mode": "strict",
                "env": env,
                "strip_python_env": True,
                "is_kiro_cli": False,
            }
            return wrapped, env, str(launcher)

        def run(argv, **options):
            assert argv == wrapped
            assert options["env"] == env
            assert options["cwd"] == str(tmp_path)
            assert launcher.exists()
            return subprocess.CompletedProcess(argv, 0, "passed", "")

        monkeypatch.setattr(lc, "sandboxed_spawn_argv", prepare)
        monkeypatch.setattr(lc, "run_limited", run)

        assert lc.run_check(lc.extract_check("Run `pytest test_example.py`"), str(tmp_path))[0]
        assert not launcher.exists()

    def test_only_allowed_environment_reaches_explicit_check(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GATEWAY_TEST_SECRET", "fixture-value")
        monkeypatch.setenv("LANG", "C")
        script = tmp_path / "verify_env.py"
        script.write_text(
            "import os\nprint(os.environ.get('LANG'))\n"
            "print('GATEWAY_TEST_SECRET' in os.environ)\n",
            encoding="utf-8",
        )
        info = SubagentInfo(id="a", task="Check the environment.")
        info.delegation = {"check": f'"{sys.executable}" verify_env.py'}
        passed, output = lc.run_check(lc.check_for(info), str(tmp_path))
        assert passed
        assert [line for line in output.splitlines() if line] == ["C", "False", "(exit 0)"]

    def test_timeout_is_a_fail(self, tmp_path):
        ok, out = lc.run_check("sleep 5", str(tmp_path), timeout=0.2)
        assert ok is False and "timed out" in out

    def test_missing_cwd_is_a_fail(self, tmp_path):
        ok, out = lc.run_check("true", str(tmp_path / "gone"))
        assert ok is False and "could not run" in out
