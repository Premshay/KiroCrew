#!/usr/bin/env python3
"""Run pytest under KiroCrew's host-wide worker and memory budget.

Two roles, one file. The LAUNCHER validates the requested worker count and
starts a memory-bounded transient service; that service re-enters this script
as the SHIM, which claims the permits and is pytest's parent for as long as the
run lasts.

The permit holder has to be inside the unit. A transient service outlives the
process that started it -- ``systemd-run --wait`` waits for the unit, it does
not own it -- so a launcher holding the locks returns them the instant it dies,
while its six workers keep running against a budget that now reads as free.
Observed exactly that way: an interrupted session left six workers on 15 GiB
with all twenty-four permits unheld. Inside the unit the lease and the process
tree it bounds end together, and the kernel releases the locks either way.

The shim is a parent rather than an ``exec``: the locks live only as long as
their file descriptors, which are not inheritable across ``exec``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent
#: Marks the re-entry the transient unit makes into this script. A sentinel
#: rather than a real option because everything after it is pytest's own argv.
SHIM_FLAG = "--hold-permits"
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


def _busy(count: int) -> str:
    return (
        f"run_agent_pytest: host test budget busy; requested {count} of "
        f"{xdist_budget._HOST_WORKER_CAP} worker permits. Retry after the "
        "other test suite exits."
    )


def contained_main(args: list[str]) -> int:
    """Hold the permits and run pytest, from inside the bounded unit.

    The count arrives already validated from the launcher; it is re-read
    defensively because this entry point is reachable by hand.
    """
    try:
        count = int(args[0])
    except (IndexError, ValueError):
        print(
            f"run_agent_pytest: {SHIM_FLAG} requires a worker count",
            file=sys.stderr,
        )
        return 64
    if not xdist_budget.claim_exact_worker_slots(count):
        print(_busy(count), file=sys.stderr)
        return 75
    # The grant stops pytest's own auto-mode hook claiming a second time
    # against a pool this process has already emptied.
    env = {**os.environ, xdist_budget._PREGRANTED_WORKERS_ENV: str(count)}
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", *args[1:]],
            cwd=REPO_ROOT,
            env=env,
            check=False,
        ).returncode
    except OSError as exc:
        print(f"run_agent_pytest: could not start pytest: {exc}", file=sys.stderr)
        return 127
    finally:
        xdist_budget.release_worker_slots()


def main(args: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)
    if argv and argv[0] == SHIM_FLAG:
        return contained_main(argv[1:])
    try:
        count = requested_workers(argv)
    except ValueError as exc:
        print(f"run_agent_pytest: {exc}", file=sys.stderr)
        return 64
    command = pytest_cgroup_scope_argv(
        [sys.executable, str(SCRIPT_PATH), SHIM_FLAG, str(count), *argv],
        working_directory=str(REPO_ROOT),
        inherit_environment=inheritable_environment(),
    )
    try:
        return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode
    except OSError as exc:
        print(f"run_agent_pytest: could not start the contained run: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
