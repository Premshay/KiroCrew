"""App helper splits retain the original import and monkeypatch surfaces."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins import code_review_sage  # noqa: F401
from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile
from kiro_crew.apps.builtins.code_review_sage.sage_lib import learning as qualified_learning
from kiro_crew.apps.builtins.code_review_sage.sage_lib import (
    review_driver as qualified_review_driver,
)

# The app bootstrap installs its standalone import path before these aliases load.
learning = importlib.import_module("sage_lib.learning")
review_driver = importlib.import_module("sage_lib.review_driver")

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.xdist_group(name="tree_scan_sync_app_facades"),
]


@pytest.mark.parametrize("facade", [learning, review_driver, profile])
def test_every_existing_app_patch_reaches_its_owner(facade) -> None:
    import facade_mirror_scan
    from test_facade_mirror_guards import _test_tree_files, patch_reach_failures

    spellings = {facade.__name__: facade.__name__}
    names = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8")
        if facade.__name__.rpartition(".")[2] not in text:
            continue
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(facade.__name__, set())
        if facade_mirror_scan.needs_syntax_tree(text, spellings):
            names |= facade_mirror_scan.patched_names(text, spellings).get(facade.__name__, set())
    assert names, f"the corpus must contain real patches of {facade.__name__}"
    known = {learning: "pattern_id", review_driver: "sys", profile: "_run"}
    assert known[facade] in names, "the scan must find the explicit moved-code seam"
    assert patch_reach_failures(facade, names) == []
    assert vars(facade)[_facade_mirror.INDEX_ATTRIBUTE].divergent() == ()


def test_learning_format_reads_the_facade_pattern_id_patch(monkeypatch) -> None:
    monkeypatch.setattr(learning, "pattern_id", lambda *args: "patched-id")
    assert learning._normalize_pattern({"title": "a rule"})["id"] == "patched-id"
    monkeypatch.setattr(qualified_learning, "pattern_id", lambda *args: "qualified-id")
    assert qualified_learning._normalize_pattern({"title": "a rule"})["id"] == "qualified-id"


def test_review_prompt_reads_the_facade_interpreter_patch(monkeypatch) -> None:
    monkeypatch.setattr(review_driver, "sys", type("Process", (), {"executable": "app-python"}))
    assert review_driver.python_command() == "app-python"
    monkeypatch.setattr(
        qualified_review_driver, "sys", type("Process", (), {"executable": "qualified-python"})
    )
    assert qualified_review_driver.python_command() == "qualified-python"


def test_profile_measurement_reads_the_facade_runner_patch(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(profile, "_run", run)
    profile._time_suite(tmp_path)
    assert len(calls) == 1
    assert calls[0][1]["cwd"] == tmp_path


@pytest.mark.parametrize("facade", [learning, review_driver, profile])
def test_app_owner_imports_never_point_back_to_the_facade(facade) -> None:
    from test_facade_mirror_guards import owner_imports_of_facade

    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    family = [facade, *(importlib.import_module(name) for name in index.owners)]
    sources = {
        module.__name__: Path(module.__file__).read_text(encoding="utf-8") for module in family
    }
    assert owner_imports_of_facade(index, sources) == []
    assert all(sys.modules[name] in family for name in index.owners)
