"""One bounded, owner-started repository assessment that survives navigation."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path

from kiro_crew.platform.app_execution import capture_app_execution
from kiro_crew.platform.context import redact_via_context

from . import clone_setup, commit, notify, store
from .assessment_evidence import assessment_prompt, capture_evidence, validate_result
from .repository_survey import _git, resolve_survey_ref

_lock = threading.Lock()
_thread: threading.Thread | None = None
_active_key = ""
_stop = threading.Event()
_TIMEOUT_S = 300


def _identity(config: dict, area_path: str = "") -> dict:
    root = Path(config.get("clone") or "")
    if not config.get("clone") or not clone_setup._repository_is_isolated(root):
        raise ValueError("Connect an isolated repository before assessing it")
    branch = str(config.get("branch") or "HEAD")
    revision = _git(
        root, "rev-parse", "--verify", resolve_survey_ref(root, branch) + "^{commit}"
    ).strip()
    key = hashlib.sha256(json.dumps([str(root), branch, revision, area_path]).encode()).hexdigest()
    return {"key": key, "branch": branch, "revision": revision, "areaPath": area_path}


def _path(key: str) -> Path:
    return store.data_dir() / "assessments" / (key + ".json")


def stop_assessment() -> None:
    """Signal the provider stop check; safe from app disable and shutdown hooks."""
    _stop.set()


def assessment_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def read_assessment(area_path: str = "") -> dict:
    config = store.read_json(store.config_path(), {}) or {}
    if not config.get("clone"):
        return {"state": "none"}
    # Ref reads are atomic; status must not wait for a long run's clone lock.
    identity = _identity(config, area_path)
    record = store.read_json(_path(identity["key"]), {}) or {}
    if not record:
        return {"state": "none", **identity}
    if record.get("state") == "assessing" and (
        not assessment_running() or _active_key != identity["key"]
    ):
        return {**record, "state": "interrupted", "progress": "Assessment was interrupted"}
    return record


def _build_runner():
    import kiro_crew.acp  # noqa: F401 — initialize before the provider factory

    from ..spine.agent_runner import SessionAgentRunner
    from .runner import _credentials_are_unconfined

    reason = _credentials_are_unconfined()
    if reason:
        raise ValueError("Repository assessment refused: " + reason)
    if not SessionAgentRunner.available():
        raise ValueError("No provider-backed assessment agent is available")
    runner = SessionAgentRunner(default_timeout_s=_TIMEOUT_S, stop_check=_stop.is_set)
    if not runner.ensure_agent_registered():
        raise ValueError("The restricted assessment agent could not be registered")
    return runner


def start_assessment(area_path: str = "") -> dict | None:
    """Capture identity, then start one worker; None means already running."""
    global _thread, _active_key
    from .routes import _run_is_active

    with commit.clone_lock():
        if _run_is_active():
            raise ValueError("Wait for the improvement run to finish before assessing")
        config = store.read_json(store.config_path(), {}) or {}
        identity = _identity(config, area_path)
        with _lock:
            if _thread is not None and _thread.is_alive():
                return None
            _stop.clear()
            _active_key = identity["key"]
            record = {
                **identity,
                "state": "assessing",
                "startedAt": time.time(),
                "progress": "Reading committed repository evidence",
            }
            store.write_json_atomic(_path(identity["key"]), record)
            _thread = threading.Thread(
                target=capture_app_execution(lambda: _work(record, config)),
                name="auto-improvement-assessment",
                daemon=True,
            )
            _thread.start()
    return record


def _work(record: dict, config: dict) -> None:
    path = _path(record["key"])
    final = dict(record)
    try:
        # The revision was pinned under the clone lock at start. Read immutable
        # objects, never the checkout; a later run cannot alter these contents.
        evidence = capture_evidence(Path(config["clone"]), record["revision"], record["areaPath"])
        if _stop.is_set():
            final.update(state="cancelled", progress="Assessment cancelled")
        else:
            runner = _build_runner()
            final["progress"] = "Investigating sampled evidence; no repository commands run"
            store.write_json_atomic(path, final)
            with tempfile.TemporaryDirectory(prefix="auto-improvement-assessment-") as cwd:
                result = runner.run(
                    assessment_prompt(evidence),
                    cwd=cwd,
                    allowed_tools=[],
                    max_turns=1,
                    timeout_s=_TIMEOUT_S,
                )
            if _stop.is_set():
                final.update(state="cancelled", progress="Assessment cancelled")
            elif not result.ok:
                raise ValueError(result.error or "Assessment agent did not complete")
            else:
                final["result"] = validate_result(result.text, evidence)
                current = store.read_json(store.config_path(), {}) or {}
                matches = _identity(current, record["areaPath"])["key"] == record["key"]
                final.update(
                    state="ready" if matches else "stale",
                    progress="Review suggestions; no configuration was changed",
                )
    except Exception as exc:  # noqa: BLE001 — persist terminal worker failures for the UI
        final.update(
            state="failed", progress="Assessment failed", error=redact_via_context(str(exc))[:2000]
        )
    final["finishedAt"] = time.time()
    store.write_json_atomic(path, final)
    notify.task_finished(
        "Repository assessment " + final["state"],
        str(final.get("error") or final["progress"]),
        group_key="repository-assessment",
    )
