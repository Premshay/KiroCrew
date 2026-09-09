#!/usr/bin/env python3
"""Run pytest under KiroCrew's host-wide worker and memory budget.

This wrapper is the parent of pytest rather than an ``exec`` shim.  It holds
the six shared worker-lock file descriptors while a waited-for systemd service
owns pytest and all xdist children, then returns the permits after reaping the
service.  A direct exec would close Python's non-inheritable descriptors and
silently release the host budget while tests still ran.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import xdist_budget  # noqa: E402  (repo paths must be present before these imports)
from kiro_crew.sandbox import pytest_cgroup_scope_argv  # noqa: E402

# Variables the service manager owns for each unit it starts. Forwarding this
# process's copies would describe the wrapper's own unit to pytest's.
_UNIT_MANAGED_ENV = frozenset(
    {
        "INVOCATION_ID",
        "JOURNAL_STREAM",
        "LISTEN_FDNAMES",
        "LISTEN_FDS",
        "LISTEN_PID",
        "MAINPID",
        "MANAGERPID",
        "MEMORY_PRESSURE_WATCH",
        "MEMORY_PRESSURE_WRITE",
        "NOTIFY_SOCKET",
        "SYSTEMD_EXEC_PID",
        "WATCHDOG_PID",
        "WATCHDOG_USEC",
    }
)


def inheritable_environment() -> list[str]:
    """Names to carry into the contained run so it matches a direct one.

    Containment exists to bound memory, not to reshape the environment. Dropping
    the caller's copy is not neutral: without ``TMPDIR`` the suite writes its
    temporary trees to the shared ``/tmp``, which fails tests that assert on
    their own scratch paths and leaves residue outside the session's directory.
    """
    return sorted(
        name
        for name in os.environ
        if name and "=" not in name and name not in _UNIT_MANAGED_ENV
    )


def requested_workers(args: list[str]) -> int:
    """Return the exact permit count for pytest's xdist options.

    No explicit option still means six: this repository's pytest configuration
    supplies ``-n auto``.  Explicit values never downsize silently; callers get
    a clear usage error instead of a command that ran with a different shape.
    """
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-n", "--numprocesses"}:
            if index + 1 >= len(args):
                raise ValueError(f"{arg} requires a worker count")
            values.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--numprocesses="):
            values.append(arg.split("=", 1)[1])
        index += 1
    if not values:
        return xdist_budget._HOST_WORKER_CAP
    if len(set(values)) != 1:
        raise ValueError("conflicting pytest worker counts")
    raw = values[0].lower()
    if raw in {"auto", "logical"}:
        return xdist_budget._HOST_WORKER_CAP
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid pytest worker count {raw!r}") from exc
    if count < 0:
        raise ValueError(f"invalid pytest worker count {raw!r}")
    # Serial pytest still occupies one cgroup/process tree and gets one permit.
    permits = max(1, count)
    if permits > xdist_budget._HOST_WORKER_CAP:
        raise ValueError(
            f"pytest worker count {count} exceeds the host limit "
            f"of {xdist_budget._HOST_WORKER_CAP}"
        )
    return permits


def main(args: list[str] | None = None) -> int:
    pytest_args = list(sys.argv[1:] if args is None else args)
    try:
        count = requested_workers(pytest_args)
    except ValueError as exc:
        print(f"run_agent_pytest: {exc}", file=sys.stderr)
        return 64
    if not xdist_budget.claim_exact_worker_slots(count):
        print(
            f"run_agent_pytest: host test budget busy; requested {count} of "
            f"{xdist_budget._HOST_WORKER_CAP} worker permits. Retry after the "
            "other test suite exits.",
            file=sys.stderr,
        )
        return 75

    granted = {xdist_budget._PREGRANTED_WORKERS_ENV: str(count)}
    # Both paths must carry the grant: the transient unit reads --setenv, and
    # the cgroup-unavailable fallback runs pytest as a direct child of this env.
    env = {**os.environ, **granted}
    command = pytest_cgroup_scope_argv(
        [sys.executable, "-m", "pytest", *pytest_args],
        working_directory=str(REPO_ROOT),
        environment=granted,
        inherit_environment=inheritable_environment(),
    )
    try:
        return subprocess.run(command, cwd=REPO_ROOT, env=env, check=False).returncode
    except OSError as exc:
        print(f"run_agent_pytest: could not start pytest: {exc}", file=sys.stderr)
        return 127
    finally:
        xdist_budget.release_worker_slots()


if __name__ == "__main__":
    raise SystemExit(main())
