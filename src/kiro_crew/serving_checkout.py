"""Detect a serving checkout that moved under the running gateway.

The deployed checkout is also this process's import root, so a fast-forward,
merge, or editor write while the gateway runs leaves it executing one revision
and importing the next. The two only disagree where an import happens late: a
module read from disk now can ask an already-loaded module for a name it did not
define when the process started, and the request that triggered the import fails
with ImportError — so the symptom reads as a bug in an unrelated feature rather
than as a deployment that moved underneath a live process.

The check is about the process, not about git: a source file whose mtime is newer
than this process's start means the two can disagree, whether the change was
committed, merged, or typed. Nothing is written; each scan is a stat walk over
the package, cached for ``CHECK_INTERVAL_SECS`` and meant to run off the event
loop. The gateway logs one warning per changed file and publishes the current
result to direct-local probes.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Import root of this process. The walk starts here, so a scan sees exactly the
#: files this process could still import from.
PACKAGE_ROOT = Path(__file__).resolve().parent

#: Minimum seconds between stat walks. The walk is a few thousand stats, cheap
#: enough for a minute's cadence and rare enough to stay invisible.
CHECK_INTERVAL_SECS = 60.0


@dataclass(frozen=True)
class CodeDrift:
    """A source file that changed after this process started."""

    path: str
    mtime: float
    #: Seconds between process start and the file's last write.
    age_secs: float

    def to_payload(self) -> dict[str, object]:
        return {"path": self.path, "age_secs": round(self.age_secs, 1)}


_current: CodeDrift | None = None
_scanned_at: float | None = None
_reported: set[tuple[str, float]] = set()
_started_at: float | None = None


def _start_from_stat(stat_path: Path) -> float | None:
    """Wall-clock start of the process *stat_path* describes; None if unreadable.

    ``stat`` field 22 is the start time in clock ticks since boot, and the
    process name it follows may contain spaces and parentheses, so the split
    happens after the LAST closing paren. Boot time comes from CLOCK_BOOTTIME,
    which shares the kernel's suspend-inclusive clock.
    """
    try:
        fields = stat_path.read_bytes().rsplit(b")", 1)[1].split()
        ticks = float(fields[19])
        boot_secs = time.clock_gettime(time.CLOCK_BOOTTIME)
        return time.time() - (boot_secs - ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, IndexError, ValueError, AttributeError):
        return None


def process_started_at() -> float:
    """Start of this process, resolved once."""
    global _started_at
    if _started_at is None:
        _started_at = _start_from_stat(Path("/proc/self/stat")) or time.time()
    return _started_at


def pid_started_at(pid: int) -> float | None:
    """Start of *pid*, or None when it is gone or unreadable.

    A freshly spawned process (the doctor CLI) cannot answer "has the checkout
    moved since the GATEWAY started?" from its own start time, so it asks about
    the gateway process instead.
    """
    return _start_from_stat(Path(f"/proc/{pid}/stat"))


def newest_source_mtime(root: Path = PACKAGE_ROOT) -> tuple[float, str]:
    """``(mtime, path)`` of the most recently written ``.py`` under *root*."""
    newest, newest_path = 0.0, ""
    for entry in root.rglob("*.py"):
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest, newest_path = mtime, str(entry)
    return newest, newest_path


def drift_since(started_at: float, *, root: Path = PACKAGE_ROOT) -> CodeDrift | None:
    """The newest source written after *started_at*, or None when none was.

    Takes its reference time as an argument and caches nothing: a fresh process
    asks about ANOTHER process's start (the gateway it is diagnosing), which is
    a different question from this process's own drift and must not overwrite
    the cached answer to that one.
    """
    newest, path = newest_source_mtime(root)
    if newest <= started_at:
        return None
    return CodeDrift(path=path, mtime=newest, age_secs=newest - started_at)


def scan(
    *,
    force: bool = False,
    now: float | None = None,
    root: Path = PACKAGE_ROOT,
) -> CodeDrift | None:
    """Return the newest drift, or None when every source predates the process.

    Cached: a call inside ``CHECK_INTERVAL_SECS`` of the last scan returns that
    scan's result without touching the filesystem. *force* bypasses the cache,
    and *now*/*root* exist for tests.
    """
    global _current, _scanned_at
    moment = time.time() if now is None else now
    if not force and _scanned_at is not None and (moment - _scanned_at) < CHECK_INTERVAL_SECS:
        return _current
    _scanned_at = moment
    _current = drift_since(process_started_at(), root=root)
    return _current


def current() -> CodeDrift | None:
    """The last scan's result, without scanning. None until a scan has run."""
    return _current


def report(drift: CodeDrift | None = None) -> bool:
    """Log *drift* once per changed file; return whether this call logged.

    A live gateway re-scans every minute, so the change is announced on the first
    scan that sees it and then stays quiet — the log line is the signal, not a
    heartbeat. The remedy is a restart; nothing here repairs the process.
    """
    found = current() if drift is None else drift
    if found is None:
        return False
    key = (found.path, found.mtime)
    if key in _reported:
        return False
    _reported.add(key)
    logger.warning(
        "Serving checkout changed under this process: %s was written %.0fs after start. "
        "Code loaded at start and code on disk now disagree — restart the gateway to serve "
        "one revision.",
        found.path,
        found.age_secs,
    )
    return True


def reset_for_tests(started_at: float | None = None) -> None:
    """Clear the cache, the reported set, and the recorded process start."""
    global _current, _scanned_at, _started_at
    _current = None
    _scanned_at = None
    _reported.clear()
    _started_at = started_at
