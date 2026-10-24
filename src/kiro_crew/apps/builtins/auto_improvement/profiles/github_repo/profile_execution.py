"""Bounded repository execution and pytest measurement helpers."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sandbox import run_limited, sandboxed_spawn_argv

from ...spine.push_policy import strip_credential_env  # noqa: E402
from .environment import TestEnvironment

logger = logging.getLogger(__name__.removesuffix("_execution"))

#: Bounded so one pathological repo cannot wedge a run. A suite slower than this is
#: not a viable measurement target anyway (a 15-minute A/B rep × 6 reps × 2 arms).
_SUITE_TIMEOUT_S = 900.0

_QUICK_TIMEOUT_S = 180.0

#: ``pytest -q`` failure-line shape: ``FAILED path::case - reason`` / ``ERROR path``.
_NODEID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")

#: ``pytest --collect-only -q`` tail: ``N tests collected in 0.12s``.
_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")

#: SGR escape sequences. pytest colorizes its summary — including the middle of a
#: nodeid (``tests/x.py::<esc>test_name<esc>``) — whenever it thinks it has a terminal
#: or the target repo's own ``addopts`` force color. We pass ``--color=no``, but a repo
#: config can override that, so every parsed line is stripped first: a nodeid with an
#: escape sequence embedded in it is not a nodeid the gate can re-run.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# ── shared helpers ───────────────────────────────────────────────────────────


#: Directory names that hold a pytest suite. They locate the RUN ROOT, which is
#: not always the directory the spine hands us — see :func:`_repo_root`.
_TEST_DIRS = ("tests", "test")


def _has_tests(tree: Path) -> bool:
    """Whether ``tree`` looks like the directory pytest should be invoked from."""
    if any((tree / name).is_dir() for name in _TEST_DIRS):
        return True
    # A repo with tests beside the code and no tests/ dir at all (``foo_test.py`` next
    # to ``foo.py``) still roots pytest here.
    return any(tree.glob("test_*.py")) or any(tree.glob("*_test.py"))


def _repo_root(tree: Path) -> Path:
    """Resolve a spine-supplied source path to the tree pytest must actually run in.

    The spine appends ``src`` to a clone/worktree unconditionally (see the module
    docstring), so ``tree`` arrives as ``<repo>/src`` whether or not that is where the
    suite lives. Two distinct layouts hide behind that one path and the difference is
    not cosmetic:

    * **flat** — no ``src/`` at all, so the path does not exist; the run root is its
      parent. (This is ``driver_publish.py``'s own ``if not src.exists(): src = clone`` idiom.)
    * **src-layout** — ``<repo>/src`` DOES exist but holds only the package, while the
      suite sits at ``<repo>/tests``. Running pytest inside ``src/`` collects nothing
      and exits 5, which every caller here would read as a RED suite. A perfectly green
      repo would be reported broken, the build gate would refuse every candidate, and
      the failure would look like the agent's fault. So when the parent is the side
      holding the tests, the parent is the run root.

    Importability is handled separately by :func:`_measure_env`, which puts BOTH the
    run root and its ``src`` on ``PYTHONPATH`` — the run root is a pytest concern, the
    package location is an import concern, and conflating them is what caused the bug
    above.
    """
    tree = Path(tree)
    if tree.name == "src" and (tree.parent / ".git").exists():
        return tree.parent
    if tree.is_dir() and _has_tests(tree):
        return tree
    parent = tree.parent
    if parent.is_dir() and _has_tests(parent):
        return parent
    # Neither side declares a suite: fall back to whichever path exists, preserving the
    # spine's own resolution order.
    if tree.exists():
        return tree
    return parent if parent.exists() else tree


def _write_protected_targets() -> tuple[str, ...]:
    """Directories to bind-mount empty so agent-authored tests cannot write Kiro Crew's config.

    Derived from ``security.write_protected_home_paths()`` rather than hardcoded, so a path
    added to the platform list is covered automatically instead of drifting. Two details that
    were wrong in the first attempt, both found by measuring rather than reasoning:

    * The PARENT DIRECTORY is masked, not the file. ``sandboxed_spawn_argv``'s
      ``extra_hidden_dirs`` reaches the launcher's ``sensitive_dirs`` loop, which is guarded
      by ``os.path.isdir(target)`` — so a FILE path is silently skipped and the mask
      no-ops. (Files are masked through a separate ``sensitive_files`` list the public helper
      does not expose.) Measured: passing the file paths left the child able to append to
      ``~/.kiro/crew/config.json`` and exit 0; passing the parent blocked it.
    * Only EXISTING directories are returned — the launcher mounts over each target, and a
      mount over a missing path is a needless failure on a fresh install.

    Masking the whole ``~/.kiro/crew`` directory is safe *for this caller* specifically: the
    sandboxed child is the TARGET REPOSITORY's pytest, which has no legitimate reason to read
    Kiro Crew's own configuration. It is a superset of the 18 per-file entries ``strict``
    already hides there, and measured not to disturb the interpreter.

    Each harness's own permission files (``native_allow_stores``) are left out: they are on
    the write tier only to refuse the agent's file-edit tools, and masking their parents
    would hide whole harness config directories (``~/.claude``, ``~/.codex`` ...) from the
    child, reads included.

    Fail-soft: if the helper is unavailable the sandbox is still applied without the extra
    masking, which beats refusing to run any test at all.
    """
    try:
        from kiro_crew.agent_sdk.native_allow_stores import home_leaves
        from kiro_crew.security import write_protected_home_paths

        home = Path.home()
        native = set(home_leaves())
        parents = {(home / rel).parent for rel in write_protected_home_paths() if rel not in native}
        return tuple(sorted(str(p) for p in parents if p.is_dir()))
    except Exception:  # noqa: BLE001 — masking is defense-in-depth, not the only control
        logger.debug("could not resolve write-protected paths to mask", exc_info=True)
        return ()


def _run(
    argv: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a bounded, non-shell subprocess. Never raises on a non-zero exit — every
    caller here reads ``returncode`` as data, not as an error.

    ROUTED THROUGH THE SANDBOX CHOKEPOINT, because this is where the harness executes
    code the AGENT just wrote: every gate step runs the target repo's pytest (and its
    conftest, and the reproducing test) inside the candidate worktree. Both the command
    and the working directory are therefore agent-influenced in exactly the sense
    ``test/test_spawn_audit.py`` cares about — the same reasoning that keeps
    ``task_executor.run_tests`` routed. ``sandboxed_spawn_argv`` adds OS-level
    filesystem isolation plus a credential-scrubbed environment, so a malicious or
    simply careless conftest cannot read the operator's credentials.

    The worktree must stay VISIBLE to the sandbox or there would be nothing to test,
    and any env this module composed (``_measure_env``: PYTHONPATH, PYTHONHASHSEED,
    the addopts override) is layered ON TOP of the scrubbed base so measurement
    determinism survives the scrub.
    """
    root = str(Path(cwd).resolve())
    # STRICT, not the default "standard": "standard" deliberately leaves ~/.aws visible so
    # a test suite can use the AWS CLI. But the code running here is agent-authored — a
    # candidate's own conftest/reproducing test — which is precisely who must not see the
    # operator's credentials. Measured on the author's host: under "standard" the child saw
    # all 7 ~/.aws entries; under "strict", 0. The agent SPAWN was already switched to
    # strict; this is the gate, the other place untrusted code executes. Raised by review.
    # Also MASK Kiro Crew's own write-protected files. `mode="strict"` hides 52 credential
    # paths so agent-authored code cannot READ secrets, but it does not make the rest of the
    # filesystem read-only — measured on this host: a strict-mode child appended to
    # `~/.kiro/crew/config.json` and exited 0. Those paths are `security.
    # write_protected_home_paths()`, enforced by the platform HOOK layer, which a sandboxed
    # subprocess never passes through — so the protection was inert for exactly the code that
    # most needs it. Bind-mounting an empty dir over each makes the write fail at the kernel
    # instead. Scoped to Kiro Crew's OWN control files (config.json, config.local.json
    # under both `.kiro/crew` and `.kirocrew`), i.e. the one-way doors that
    # would corrupt the installation; broader hiding is not possible here because the
    # interpreter's own stdlib can live under `$HOME` (measured: hiding `~/.local/share`
    # broke `import platform` outright). Raised by the GPT review of this branch.
    sandboxed, scrubbed_env, cleanup = sandboxed_spawn_argv(
        list(argv),
        mode="strict",
        extra_visible_dirs=(root,),
        extra_hidden_dirs=_write_protected_targets(),
    )
    if env:
        scrubbed_env.update(env)
    # Belt and braces: strip credential-shaped names AFTER layering, so neither the
    # caller's dict nor a gap in the shared scrub list can hand a token to agent-authored
    # test code. See `_CREDENTIAL_ENV_MARKERS`.
    scrubbed_env = strip_credential_env(scrubbed_env)
    try:
        # Kernel RLIMIT ceiling (NPROC/NOFILE/CPU/AS) on top of the sandbox: a
        # runaway conftest or a fork bomb in the agent's own test cannot exhaust
        # the host running the gateway. run_limited delivers the limits after
        # exec via the spawn shim instead of in a fork child of this threaded
        # process.
        return run_limited(
            sandboxed,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            shell=False,
            env=scrubbed_env,
        )
    finally:
        # A temp launcher/profile FILE, per sandboxed_spawn_argv's contract — unlink it,
        # matching task_executor.run_tests and source_providers._run_json.
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


#: Host variables the measurement subprocess genuinely needs. An ALLOWLIST, so adding a
#: name is a visible decision rather than a side effect of inheriting the whole
#: environment. Deliberately excludes every credential-shaped family (AWS_*, GITHUB_*,
#: *_TOKEN, SSH_AUTH_SOCK, …): the sandbox scrubs those, and `_run` layers this dict on
#: top of the scrubbed env, so anything listed here defeats that scrub.
_MEASURE_ENV_PASSTHROUGH = (
    "PATH",  # without it a subprocess cannot find python/pytest at all
    "HOME",  # pytest/pip cache locations; a missing HOME breaks many suites
    "TMPDIR",  # tmp_path fixtures
    "LANG",  # text decoding in test output
    "LC_ALL",
    "LC_CTYPE",
    "TZ",  # date-sensitive assertions must not shift between arms
    "TERM",  # some suites probe it; absent TERM makes output differ between arms
    "SYSTEMROOT",  # Windows: CPython needs it to initialize
    "COMSPEC",  # Windows
    # Memory-aware cap for xdist's ``-n auto`` when the selected environment
    # supplies the plugin. Seeded at the agent spawn
    # boundary (see resource_status.inject_xdist_auto_cap); without this
    # passthrough the allowlist would strip it and the suite would size to the
    # CPU count regardless of memory. Same value for both A/B arms, so arm
    # fairness is preserved. Not credential-shaped.
    "PYTEST_XDIST_AUTO_NUM_WORKERS",
)


def _measure_env(tree: Path) -> dict[str, str]:
    """The measurement environment, held byte-identical across both A/B arms.

    ``PYTHONHASHSEED=0`` and ``PYTHONDONTWRITEBYTECODE=1`` are the two conditions
    that otherwise silently differ between arms: a random hash seed changes dict/set
    iteration order (and with it any order-sensitive timing), and bytecode caching
    makes whichever arm runs FIRST pay compilation the other one skips. Pinning both
    is what makes the interleaved A/B a fair comparison rather than a coin flip.
    """
    # ONLY the explicit measurement variables — never a copy of `os.environ`. The caller
    # (`_run`) layers this dict ON TOP of the sandbox's credential-scrubbed environment,
    # so anything inherited here is put straight back after the sandbox removed it.
    # Measured before fixing: an `AWS_SECRET_ACCESS_KEY` scrubbed by the sandbox
    # reappeared in the child's env once this dict was applied — handing the operator's
    # credentials to agent-authored test code. Raised by review of this branch.
    #
    # A few HOST variables are still required for a subprocess to run at all (PATH) or to
    # behave like a normal user session (HOME, TMPDIR, locale). Those are matched against
    # the named allowlist above via the shared convention (exact on POSIX, case-folded on
    # Windows — see platform_compat.env_key_allowed), so the set is auditable and cannot
    # silently grow to include a credential.
    env: dict[str, str] = {
        name: value
        for name, value in os.environ.items()
        if platform_compat.env_key_allowed(name, _MEASURE_ENV_PASSTHROUGH)
    }
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Prepend the tree so the candidate's OWN source is imported, not an installed copy
    # of the same package that happens to be on the path. ``<tree>/src`` goes on too
    # when it exists: in a src-layout repo the run root is the repo root (that is where
    # the suite is) but the package is one level down, and an uninstalled src-layout
    # repo is not importable without it.
    roots = [str(tree)]
    inner = Path(tree) / "src"
    if inner.is_dir():
        roots.append(str(inner))
    existing = env.get("PYTHONPATH", "")
    if existing:
        roots.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(roots)
    # Drop any PYTEST_ADDOPTS INHERITED from the gateway's environment, so our runs do
    # not silently pick up flags the operator's shell happened to export (the arms must
    # be byte-identical and repo-independent). This does NOT neutralize the target
    # repo's ini ``addopts`` — an env value is APPENDED to the ini one, not a
    # replacement — that is done with ``-o addopts=`` in :func:`_pytest_argv`.
    env["PYTEST_ADDOPTS"] = ""
    return env


def _xdist_argv(environment: TestEnvironment, root: Path) -> tuple[str, ...]:
    probe = (
        "import sys\n"
        "try: import xdist\n"
        "except ModuleNotFoundError as exc:\n"
        " if exc.name != 'xdist': raise\n"
        " print('KIRO_XDIST_ABSENT'); sys.exit(3)\n"
    )
    proc = environment.run(
        environment.python_argv("-c", probe),
        cwd=root,
        timeout=_QUICK_TIMEOUT_S,
        env=_measure_env(root),
    )
    if proc.returncode == 0:
        return ("-n", "auto")
    if proc.returncode == 3 and (proc.stdout or "").strip() == "KIRO_XDIST_ABSENT":
        return ()
    raise RuntimeError(
        "Could not check pytest-xdist; repair the selected test environment and check again: "
        + json.dumps(_diagnostic("xdist", proc))
    )


#: Substrings that mean xdist itself failed to run, as opposed to tests failing.
#: They trigger a serial retry of the suite so the gate still gets a real verdict.
_XDIST_FAILURE_MARKERS = (
    "unrecognized arguments: -n",
    "error: unrecognized arguments",
    "Different tests were collected",
    "node down",
    "worker crashed",
    "Replacing crashed worker",
)


def _looks_like_xdist_failure(stdout: str, stderr: str) -> bool:
    """True when the run failed because of xdist, not because tests failed."""
    blob = f"{stdout}\n{stderr}"
    return any(m in blob for m in _XDIST_FAILURE_MARKERS)


def _suite_scope_for_globs(clone: Path, globs: list[str] | None) -> list[str]:
    """The test PATHS the gate suite should run, given a NARROWED edit allowlist.

    Empty (→ run the whole tree) unless the operator confined edits to a subtree. When
    they did, gating a one-subtree change against the ENTIRE repo suite is both
    infeasible and pointless: this monorepo collects 21,901 tests that cannot finish
    inside the suite timeout even in parallel, while the edit fence already guarantees
    the change touches only the subtree — so a regression it can cause is one its own
    subtree's tests would catch. We therefore scope the STAYGREEN/build suite to the
    ``tests``/``test`` directory nearest the edit region (28s vs a 900s timeout here).

    Derivation: take the common ANCESTOR directory of the edit globs (strip the glob
    tail), then use the first of ``<ancestor>/tests`` / ``<ancestor>/test`` that exists.
    Returns [] — meaning "no scope, run everything" — when no allowlist is set, the
    globs share no ancestor, or no test dir is found there (fail OPEN to the safe,
    whole-tree behavior rather than silently gating nothing).
    """
    if not globs:
        return []
    # Directory part of each glob (drop the ``*.py`` / ``**`` tail).
    dirs: list[tuple[str, ...]] = []
    for g in globs:
        head = g.split("*", 1)[0]  # everything before the first glob metachar
        parts = tuple(p for p in head.split("/") if p and p != ".")
        # Drop a trailing partial filename fragment (a glob like ``foo/bar_*.py`` →
        # head ``foo/bar_`` → the ``bar_`` piece is not a directory).
        if parts and not (clone / Path(*parts)).is_dir():
            parts = parts[:-1]
        if parts:
            dirs.append(parts)
    if not dirs:
        return []
    # Common ancestor of all edit dirs.
    ancestor = dirs[0]
    for d in dirs[1:]:
        common: list[str] = []
        for a, b in zip(ancestor, d):
            if a != b:
                break
            common.append(a)
        ancestor = tuple(common)
        if not ancestor:
            return []
    # Walk UP from the common ancestor to the NEAREST enclosing test dir. A single
    # glob (``.../backend/*.py``) has the edited dir itself as its ancestor, and test
    # dirs sit beside a package rather than inside it — without the walk that common
    # case would fail open and re-run the whole repo. Walking up is safe by
    # construction: each step widens the suite, and the last candidate is the repo's
    # own top-level test dir (i.e. the original whole-tree behavior, just explicit).
    walk: list[str] = list(ancestor)
    while True:
        base = clone / Path(*walk) if walk else clone
        for name in ("tests", "test"):
            if (base / name).is_dir():
                # REPO-RELATIVE so it composes with cwd=_repo_root regardless of the
                # src-layout ``src`` prepend.
                return [str(Path(*walk) / name) if walk else name]
        if not walk:  # checked the repo root — nothing above it
            return []
        walk.pop()


def _pytest_argv(*args: str, environment: TestEnvironment | None = None) -> list[str]:
    """Run the selected interpreter with deterministic pytest options."""
    return [
        *(
            environment.python_argv("-m", "pytest")
            if environment
            else [sys.executable, "-m", "pytest"]
        ),
        "-p",
        "no:cacheprovider",
        "--color=no",
        "-o",
        "addopts=",
        *args,
    ]


def _time_suite(
    tree: Path,
    *,
    extra: tuple[str, ...] = (),
    timeout: float = _SUITE_TIMEOUT_S,
    environment: TestEnvironment | None = None,
    run: Callable | None = None,
) -> tuple[float, bool]:
    """Wall-clock one full suite run in ``tree``. Returns ``(seconds, passed)``.

    ``time.perf_counter`` around the subprocess, not pytest's own reported duration:
    interpreter start and collection are real costs a candidate can improve (or
    regress), and pytest's summary excludes them.
    """
    root = _repo_root(tree)
    argv = _pytest_argv("-q", *extra, environment=environment)
    t0 = time.perf_counter()
    try:
        proc = (run or (environment.run if environment else _run))(
            argv, cwd=root, timeout=timeout, env=_measure_env(root)
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        # A timeout/spawn failure is not a slow run — it is no measurement at all.
        return float("nan"), False
    elapsed = time.perf_counter() - t0
    # pytest exit 0 = all passed, 5 = no tests collected. Treat 5 as "not passed":
    # an empty suite provides no correctness signal to guardrail on.
    return elapsed, proc.returncode == 0


def _collected_count(
    tree: Path, environment: TestEnvironment | None = None, run: Callable | None = None
) -> int:
    """Number of tests pytest collects in ``tree``, or -1 when it cannot be read.

    This is the RH guard's raw material: a candidate that made the suite faster by
    deleting tests changes this number, and nothing else in the pipeline would see it.
    """
    root = _repo_root(tree)
    try:
        proc = (run or (environment.run if environment else _run))(
            _pytest_argv("-q", "--collect-only", environment=environment),
            cwd=root,
            timeout=_QUICK_TIMEOUT_S,
            env=_measure_env(root),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1
    if proc.returncode != 0:
        return -1
    stdout = _ANSI_RE.sub("", proc.stdout or "")
    match = _COLLECTED_RE.search(stdout)
    if match:
        return int(match.group(1))
    # Older/quieter pytest output: count the collected nodeids instead.
    ids = [ln for ln in stdout.splitlines() if "::" in ln]
    return len(ids) if ids else -1


def _failing_nodeids(stdout: str) -> list[str]:
    """Pull ``FAILED``/``ERROR`` nodeids out of ``pytest -q`` output.

    Exact nodeids matter: the spine's STAYGREEN check re-runs them against BASE to
    decide whether a failure is pre-existing (not the candidate's fault) or a real
    regression. When nothing parses, the caller must NOT report an empty list —
    see :meth:`PytestBugRunner.run_suite`.

    Escape sequences are stripped before matching. ``--color=no`` normally prevents
    them, but the target repo's own ``addopts``/``force_color`` can put them back, and
    pytest colors the test name in the MIDDLE of the nodeid — so an unstripped line
    yields no match at all, and STAYGREEN would then read "no pre-existing failures"
    on a repo that has them.
    """
    out: list[str] = []
    for line in (stdout or "").splitlines():
        match = _NODEID_RE.match(_ANSI_RE.sub("", line).strip())
        if match:
            out.append(match.group(1))
    return out


def _diagnostic(stage: str, result) -> dict:
    def clean(value) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return redact_via_context(str(value or ""))[:4000]

    return {
        "stage": stage,
        "returncode": getattr(result, "returncode", None),
        "stdout": clean(getattr(result, "stdout", "")),
        "stderr": clean(
            getattr(result, "stderr", "") or (str(result) if isinstance(result, Exception) else "")
        ),
    }


__all__ = (
    "_ANSI_RE",
    "_COLLECTED_RE",
    "_MEASURE_ENV_PASSTHROUGH",
    "_NODEID_RE",
    "_QUICK_TIMEOUT_S",
    "_SUITE_TIMEOUT_S",
    "_TEST_DIRS",
    "_XDIST_FAILURE_MARKERS",
    "_collected_count",
    "_diagnostic",
    "_failing_nodeids",
    "_has_tests",
    "_looks_like_xdist_failure",
    "_measure_env",
    "_pytest_argv",
    "_repo_root",
    "_run",
    "_suite_scope_for_globs",
    "_time_suite",
    "_write_protected_targets",
    "_xdist_argv",
    "logger",
)
