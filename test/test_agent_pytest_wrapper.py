"""Tests for the parent-held pytest worker and cgroup wrapper."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "run_agent_pytest.py"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("run_agent_pytest", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wrapper():
    return _load_wrapper()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], 6),
        (["-n", "auto"], 6),
        (["--numprocesses=logical"], 6),
        (["-n", "4"], 4),
        (["-n", "0"], 1),
    ],
)
def test_requested_workers(wrapper, args: list[str], expected: int) -> None:
    assert wrapper.requested_workers(args) == expected


@pytest.mark.parametrize("args", [["-n", "7"], ["-n", "-1"], ["-n", "nope"]])
def test_requested_workers_refuses_unsafe_counts(wrapper, args: list[str]) -> None:
    with pytest.raises(ValueError):
        wrapper.requested_workers(args)


def test_wrapper_holds_permits_until_the_scoped_pytest_returns(wrapper, monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(wrapper.xdist_budget, "claim_exact_worker_slots", lambda count: events.append(("claim", count)) or True)
    monkeypatch.setattr(wrapper.xdist_budget, "release_worker_slots", lambda: events.append("release"))
    monkeypatch.setattr(
        wrapper,
        "pytest_cgroup_scope_argv",
        lambda argv, **_kwargs: ["systemd-run", "--wait", "--", *argv],
    )

    def fake_run(command, **kwargs):
        events.append(("run", command, kwargs))
        assert "release" not in events
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)

    assert wrapper.main(["-n", "4", "test/test_xdist_host_budget.py"]) == 3
    assert events[0] == ("claim", 4)
    assert events[-1] == "release"
    _run = events[1]
    assert isinstance(_run, tuple) and _run[0] == "run"
    assert _run[2]["env"]["PREGRANTED_WORKERS"] == "4"
    assert _run[1][:3] == ["systemd-run", "--wait", "--"]


def test_wrapper_grants_workers_to_the_contained_child(wrapper, monkeypatch) -> None:
    """The grant reaches pytest through the scope's own environment, not only
    through this process's: systemd-run does not inherit the caller's env."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(wrapper.xdist_budget, "claim_exact_worker_slots", lambda count: True)
    monkeypatch.setattr(wrapper.xdist_budget, "release_worker_slots", lambda: None)

    def fake_scope(argv, **kwargs):
        seen["kwargs"] = kwargs
        return argv

    monkeypatch.setattr(wrapper, "pytest_cgroup_scope_argv", fake_scope)
    monkeypatch.setattr(
        wrapper.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 0)
    )

    assert wrapper.main(["-n", "3"]) == 0
    assert seen["kwargs"]["environment"] == {"PREGRANTED_WORKERS": "3"}
    assert "TMPDIR" in seen["kwargs"]["inherit_environment"]


def test_inheritable_environment_skips_the_wrappers_own_unit_variables(
    wrapper, monkeypatch
) -> None:
    """Unit-scoped variables describe the wrapper's service, not pytest's."""
    monkeypatch.setenv("TMPDIR", "/scratch/session")
    monkeypatch.setenv("INVOCATION_ID", "cafe")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify")

    names = wrapper.inheritable_environment()

    assert "TMPDIR" in names
    assert "INVOCATION_ID" not in names and "NOTIFY_SOCKET" not in names


def test_wrapper_fails_busy_without_launching_pytest(wrapper, monkeypatch, capsys) -> None:
    monkeypatch.setattr(wrapper.xdist_budget, "claim_exact_worker_slots", lambda count: False)
    monkeypatch.setattr(wrapper.subprocess, "run", lambda *_a, **_kw: pytest.fail("must not run"))

    assert wrapper.main(["-n", "6"]) == 75
    assert "host test budget busy" in capsys.readouterr().err


def test_wrapper_releases_permits_when_the_scope_cannot_start(wrapper, monkeypatch) -> None:
    released: list[bool] = []
    monkeypatch.setattr(wrapper.xdist_budget, "claim_exact_worker_slots", lambda count: True)
    monkeypatch.setattr(wrapper.xdist_budget, "release_worker_slots", lambda: released.append(True))
    monkeypatch.setattr(wrapper, "pytest_cgroup_scope_argv", lambda argv, **_kwargs: argv)
    monkeypatch.setattr(wrapper.subprocess, "run", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("nope")))

    assert wrapper.main([]) == 127
    assert released == [True]
