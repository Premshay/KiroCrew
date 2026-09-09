"""Tests for the pytest worker-permit and cgroup wrapper."""

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


def test_launcher_claims_nothing_and_hands_the_count_to_the_contained_shim(
    wrapper, monkeypatch
) -> None:
    """The permit holder must live INSIDE the unit it bounds.

    A transient unit outlives the process that started it, so a launcher that
    held the permits would return them the moment it died -- while its six
    workers kept running against a budget that now reads as free.
    """
    monkeypatch.setattr(
        wrapper.xdist_budget,
        "claim_exact_worker_slots",
        lambda count: pytest.fail("the launcher must not hold permits"),
    )
    seen: dict[str, object] = {}

    def fake_scope(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return ["systemd-run", "--wait", "--", *argv]

    monkeypatch.setattr(wrapper, "pytest_cgroup_scope_argv", fake_scope)
    monkeypatch.setattr(
        wrapper.subprocess, "run", lambda command, **_kw: subprocess.CompletedProcess(command, 3)
    )

    assert wrapper.main(["-n", "4", "test/test_xdist_host_budget.py"]) == 3
    argv = seen["argv"]
    assert argv[1] == str(_SCRIPT)
    assert argv[2:5] == [wrapper.SHIM_FLAG, "4", "-n"]
    assert "TMPDIR" in seen["kwargs"]["inherit_environment"]


def test_contained_shim_holds_permits_until_pytest_returns(wrapper, monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        wrapper.xdist_budget,
        "claim_exact_worker_slots",
        lambda count: events.append(("claim", count)) or True,
    )
    monkeypatch.setattr(
        wrapper.xdist_budget, "release_worker_slots", lambda: events.append("release")
    )

    def fake_run(command, **kwargs):
        events.append(("run", command, kwargs))
        assert "release" not in events
        return subprocess.CompletedProcess(command, 5)

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)

    assert wrapper.main([wrapper.SHIM_FLAG, "4", "-n", "4", "test/test_xdist_host_budget.py"]) == 5
    assert events[0] == ("claim", 4)
    assert events[-1] == "release"
    _, command, kwargs = events[1]
    assert command[1:] == ["-m", "pytest", "-n", "4", "test/test_xdist_host_budget.py"]
    assert kwargs["env"]["PREGRANTED_WORKERS"] == "4"


def test_contained_shim_refuses_a_worker_count_it_cannot_read(wrapper, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        wrapper.subprocess, "run", lambda *_a, **_kw: pytest.fail("must not run pytest")
    )

    assert wrapper.main([wrapper.SHIM_FLAG, "nine"]) == 64
    assert "worker count" in capsys.readouterr().err


def test_inheritable_environment_skips_the_launchers_own_unit_variables(
    wrapper, monkeypatch
) -> None:
    """Unit-scoped variables describe the launcher's service, not pytest's."""
    monkeypatch.setenv("TMPDIR", "/scratch/session")
    monkeypatch.setenv("INVOCATION_ID", "cafe")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify")

    names = wrapper.inheritable_environment()

    assert "TMPDIR" in names
    assert "INVOCATION_ID" not in names and "NOTIFY_SOCKET" not in names


def test_contained_shim_fails_busy_without_launching_pytest(wrapper, monkeypatch, capsys) -> None:
    monkeypatch.setattr(wrapper.xdist_budget, "claim_exact_worker_slots", lambda count: False)
    monkeypatch.setattr(wrapper.subprocess, "run", lambda *_a, **_kw: pytest.fail("must not run"))

    assert wrapper.main([wrapper.SHIM_FLAG, "6"]) == 75
    assert "host test budget busy" in capsys.readouterr().err


def test_launcher_refuses_an_unsafe_count_before_starting_anything(
    wrapper, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(wrapper.subprocess, "run", lambda *_a, **_kw: pytest.fail("must not run"))

    assert wrapper.main(["-n", "9"]) == 64
    assert "exceeds the host limit" in capsys.readouterr().err


def test_contained_shim_releases_permits_when_pytest_cannot_start(wrapper, monkeypatch) -> None:
    released: list[bool] = []
    monkeypatch.setattr(wrapper.xdist_budget, "claim_exact_worker_slots", lambda count: True)
    monkeypatch.setattr(wrapper.xdist_budget, "release_worker_slots", lambda: released.append(True))
    monkeypatch.setattr(
        wrapper.subprocess, "run", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("nope"))
    )

    assert wrapper.main([wrapper.SHIM_FLAG, "6"]) == 127
    assert released == [True]


def test_launcher_reports_a_scope_that_cannot_start(wrapper, monkeypatch, capsys) -> None:
    monkeypatch.setattr(wrapper, "pytest_cgroup_scope_argv", lambda argv, **_kwargs: argv)
    monkeypatch.setattr(
        wrapper.subprocess, "run", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("nope"))
    )

    assert wrapper.main([]) == 127
    assert "could not start" in capsys.readouterr().err
