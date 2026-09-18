"""Tests for the serving-checkout drift guard."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from kiro_crew import serving_checkout


def _write(root: Path, name: str, *, mtime: float) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 1\n")
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture(autouse=True)
def _clean_guard() -> None:
    serving_checkout.reset_for_tests()


def test_scan_is_clean_when_every_source_predates_the_process(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path, "old.py", mtime=now - 600)
    serving_checkout.reset_for_tests(started_at=now - 300)

    assert serving_checkout.scan(force=True, now=now, root=tmp_path) is None


def test_scan_reports_the_newest_source_written_after_the_process_started(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path, "old.py", mtime=now - 600)
    changed = _write(tmp_path, "changed.py", mtime=now - 30)
    serving_checkout.reset_for_tests(started_at=now - 300)

    drift = serving_checkout.scan(force=True, now=now, root=tmp_path)

    assert drift is not None
    assert drift.path == str(changed)
    assert drift.age_secs == pytest.approx(270.0, abs=1.0)
    assert serving_checkout.current() == drift


def test_scan_ignores_a_change_that_arrives_inside_the_cache_window(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path, "old.py", mtime=now - 600)
    serving_checkout.reset_for_tests(started_at=now - 300)
    assert serving_checkout.scan(force=True, now=now, root=tmp_path) is None

    _write(tmp_path, "arrived.py", mtime=now + 1)

    assert serving_checkout.scan(now=now + 2, root=tmp_path) is None
    assert serving_checkout.scan(force=True, now=now + 2, root=tmp_path) is not None


def test_report_logs_once_per_changed_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    now = time.time()
    _write(tmp_path, "changed.py", mtime=now)
    serving_checkout.reset_for_tests(started_at=now - 60)
    drift = serving_checkout.scan(force=True, now=now, root=tmp_path)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.serving_checkout"):
        assert serving_checkout.report(drift) is True
        assert serving_checkout.report(drift) is False

    assert sum("Serving checkout changed" in r.message for r in caplog.records) == 1


def test_report_is_silent_without_drift() -> None:
    assert serving_checkout.report(None) is False


def test_process_start_is_this_process_not_import_time() -> None:
    started = serving_checkout.process_started_at()

    assert 0 < started <= time.time()


def test_drift_since_compares_against_the_reference_it_is_given(tmp_path: Path) -> None:
    now = time.time()
    changed = _write(tmp_path, "changed.py", mtime=now - 30)

    # A reference later than the newest write means nothing moved after it.
    assert serving_checkout.drift_since(now + 10, root=tmp_path) is None

    drift = serving_checkout.drift_since(now - 300, root=tmp_path)

    assert drift is not None
    assert drift.path == str(changed)
    assert drift.age_secs == pytest.approx(270.0, abs=1.0)


def test_drift_since_leaves_the_cached_process_scan_alone(tmp_path: Path) -> None:
    """The doctor asks about another process; that must not answer for this one.

    ``scan`` caches what the dashboard publishes, so a doctor call that wrote
    through to it would make the gateway's own payload report a drift measured
    against a different process's start.
    """
    now = time.time()
    _write(tmp_path, "changed.py", mtime=now - 30)
    serving_checkout.reset_for_tests(started_at=now - 300)
    cached = serving_checkout.scan(force=True, now=now, root=tmp_path)
    assert cached is not None

    serving_checkout.drift_since(now - 10, root=tmp_path)

    assert serving_checkout.current() == cached


def test_pid_started_at_matches_this_process() -> None:
    assert serving_checkout.pid_started_at(os.getpid()) == pytest.approx(
        serving_checkout.process_started_at(), abs=1.0
    )


def test_pid_started_at_is_none_for_a_process_that_does_not_exist() -> None:
    """A gateway that exits between the pid read and the stat read is reported
    as unknown, not as an exception out of the doctor command."""
    assert serving_checkout.pid_started_at(0) is None
