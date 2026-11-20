"""A custom agent ("seat") on Claude Code or Codex gets its spec's absolute ``file://`` documents.

Neither harness reads an agent's ``resources``; kiro-cli loads them natively and a
crew member's essentials envelope carries its own. A seat such as
``crew-claude-atlas`` used to get only its ``prompt``, so a shared guide named by
absolute path never reached it. ``inclusion.steering_resources_part`` now loads
the seat's ABSOLUTE entries (``agent_spec._load_seat_resources``) at session start
and after a compaction, through one gate; relative entries keep their old
behaviour (not loaded), and members and kiro-cli are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.acp.types import PROVIDER_LABEL_CODEX, PROVIDER_LABEL_KAS
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE
import kiro_crew.agent as agent_mod
from kiro_crew.config import paths as paths_mod
from kiro_crew.context import _MULTIBYTE_TABLE, ContextBuilder
from kiro_crew.context_assembly import inclusion
from kiro_crew.context_assembly.agent_spec import _load_seat_resources, seat_loads_resource
from kiro_crew.dashboard import chat_runner
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_SEAT = "crew-claude-atlas"
_GUIDE_RULE = "SEAT_DELEGATION_GUIDE_K9"
_TREE_RULE = "PROJECT_TREE_RULE_P4"
_START = "[Steering resources]"
_REINJECTED = "[REINJECTED AFTER COMPACTION — steering resources]".translate(_MULTIBYTE_TABLE)
# "antigravity" is the label the companion's AntigravityHeadlessProvider reports; it is no
# ACP backend (PROVIDER_LABELS_CREW_LOADS_SPEC_STEERING), and agy reads no spec resources.
_PROVIDER_LABEL_ANTIGRAVITY = "antigravity"
_SEAT_HARNESSES = [PROVIDER_CLAUDE_CODE, PROVIDER_LABEL_CODEX, _PROVIDER_LABEL_ANTIGRAVITY]


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test here builds a ``ContextBuilder``: close its ``SkillsLoader``."""


@pytest.fixture
def home(tmp_path, monkeypatch):
    agents = tmp_path / ".kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    with patch("pathlib.Path.home", return_value=tmp_path):
        yield tmp_path


@pytest.fixture
def guide(home):
    docs = home / "repos" / "nexus" / "docs" / "crewmates"
    docs.mkdir(parents=True)
    path = docs / "delegation.md"
    path.write_text(f"# Delegation\n{_GUIDE_RULE}\n", encoding="utf-8")
    return path


@pytest.fixture
def project(home):
    proj = home / "proj"
    steering = proj / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "tree.md").write_text(f"# Tree\n{_TREE_RULE}\n", encoding="utf-8")
    return proj


def _write_seat(home, resources, name=_SEAT):
    spec = {"name": name, "prompt": "seat prompt", "resources": resources}
    (home / ".kiro" / "agents" / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _builder(home) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=home / "ws"),
        skills=SkillsLoader(skills_path=home / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=home),
    )


def _start(home, **kwargs) -> str:
    msg, _ = _builder(home).build_message("hello", is_new_session=True, **kwargs)
    return msg


def _reinjected(home, **kwargs) -> str:
    msg, _ = _builder(home).build_message(
        "carry on", is_new_session=False, needs_reinjection=True, **kwargs
    )
    return msg


def _part(**kwargs) -> str:
    args = {"agent": _SEAT, "provider_type": PROVIDER_CLAUDE_CODE, "essentials": ""}
    args.update(kwargs)
    return inclusion.steering_resources_part(context_groups=None, **args)


# --- start and compaction share the gate --------------------------------------


@pytest.mark.parametrize("provider_type", _SEAT_HARNESSES)
def test_a_seat_gets_its_absolute_resource_at_session_start(home, guide, provider_type):
    _write_seat(home, [f"file://{guide}"])
    msg = _start(home, provider_type=provider_type, agent=_SEAT)
    assert msg.count(_GUIDE_RULE) == 1
    assert msg.index(_START) < msg.index(_GUIDE_RULE)


@pytest.mark.parametrize("provider_type", _SEAT_HARNESSES)
def test_a_seat_gets_its_absolute_resource_back_after_a_compaction(home, guide, provider_type):
    _write_seat(home, [f"file://{guide}"])
    msg = _reinjected(home, provider_type=provider_type, agent=_SEAT)
    assert msg.count(_GUIDE_RULE) == 1
    assert msg.count(_REINJECTED) == 1


def test_a_glob_in_an_absolute_entry_tail_is_loaded(home, guide):
    _write_seat(home, [f"file://{guide.parent}/*.md"])
    assert _GUIDE_RULE in _part()


# --- what does not change -------------------------------------------------------


def test_a_relative_glob_resource_is_not_newly_loaded(home, guide, project):
    _write_seat(home, ["file://.kiro/steering/**/*.md", f"file://{guide}"])
    text = _part(project=str(project))
    assert _GUIDE_RULE in text
    assert _TREE_RULE not in text
    msg = _start(home, provider_type=PROVIDER_CLAUDE_CODE, agent=_SEAT, project=str(project))
    assert _TREE_RULE not in msg.split(_START, 1)[-1].split("[End of steering resources]")[0]


def test_a_home_anchored_resource_is_not_newly_loaded(home, guide):
    _write_seat(home, ["file://~/repos/nexus/docs/crewmates/delegation.md"])
    assert _part() == ""


def test_a_seat_with_only_relative_resources_gets_no_block(home, project):
    _write_seat(home, ["file://.kiro/steering/**/*.md"])
    assert _part(project=str(project)) == ""
    msg = _reinjected(home, provider_type=PROVIDER_CLAUDE_CODE, agent=_SEAT, project=str(project))
    assert _REINJECTED not in msg


@pytest.mark.parametrize("provider_type", [PROVIDER_ACP, PROVIDER_LABEL_KAS])
def test_harnesses_that_read_resources_natively_get_nothing(home, guide, provider_type):
    _write_seat(home, [f"file://{guide}"])
    assert _part(provider_type=provider_type) == ""
    assert _GUIDE_RULE not in _start(home, provider_type=provider_type, agent=_SEAT)
    assert _GUIDE_RULE not in _reinjected(home, provider_type=provider_type, agent=_SEAT)


def test_a_member_chat_gets_nothing_from_this_load(home, guide):
    _write_seat(home, [f"file://{guide}"])
    assert _part(essentials="[member essentials]") == ""


def test_a_withheld_project_group_gets_nothing(home, guide):
    _write_seat(home, [f"file://{guide}"])
    assert (
        inclusion.steering_resources_part(
            agent=_SEAT,
            provider_type=PROVIDER_CLAUDE_CODE,
            essentials="",
            context_groups=frozenset({inclusion.CONTEXT_GROUP_MEMORY}),
        )
        == ""
    )


def test_the_default_agent_load_is_unchanged(home, guide):
    """The default agent still gets its own spec's entries plus the trees, not a seat's."""
    _write_seat(home, [f"file://{guide}"])
    assert _GUIDE_RULE not in _part(agent=None)


# --- admission and dedupe -------------------------------------------------------


def test_a_document_two_entries_match_is_sent_once(home, guide):
    _write_seat(home, [f"file://{guide}", f"file://{guide.parent}/*.md"])
    assert _part().count(_GUIDE_RULE) == 1


def test_codex_leaves_out_the_project_agents_md_it_reads_itself(home, project):
    (project / "AGENTS.md").write_text("AGENTS_MD_RULE\n", encoding="utf-8")
    _write_seat(home, [f"file://{project / 'AGENTS.md'}"])
    assert "AGENTS_MD_RULE" in _part(project=str(project))
    assert _part(project=str(project), provider_type=PROVIDER_LABEL_CODEX) == ""


def test_a_symlinked_resource_is_refused_with_a_warning(home, guide, caplog):
    link = home / "link.md"
    link.symlink_to(guide)
    _write_seat(home, [f"file://{link}"])
    with caplog.at_level(logging.WARNING, logger="kiro_crew.context"):
        assert _part() == ""
    assert any("symlink" in r.getMessage() for r in caplog.records)


def test_a_symlinked_directory_in_the_path_is_refused(home, guide, caplog):
    linked_dir = home / "linked"
    linked_dir.symlink_to(guide.parent, target_is_directory=True)
    _write_seat(home, [f"file://{linked_dir / 'delegation.md'}"])
    with caplog.at_level(logging.WARNING, logger="kiro_crew.context"):
        assert _part() == ""
    assert any("symlink" in r.getMessage() for r in caplog.records)


def test_a_missing_resource_degrades_with_a_warning_and_keeps_the_rest(home, guide, caplog):
    _write_seat(home, [f"file://{home / 'nope.md'}", f"file://{guide}"])
    with caplog.at_level(logging.WARNING, logger="kiro_crew.context"):
        assert _GUIDE_RULE in _part()
    assert any("matched no file" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_an_unreadable_resource_degrades_with_a_warning(home, guide, caplog):
    guide.chmod(0)
    try:
        _write_seat(home, [f"file://{guide}"])
        with caplog.at_level(logging.WARNING, logger="kiro_crew.context"):
            assert _part() == ""
        assert any("unreadable" in r.getMessage() for r in caplog.records)
    finally:
        guide.chmod(0o644)


def test_an_unreadable_spec_loads_nothing(home):
    (home / ".kiro" / "agents" / f"{_SEAT}.json").write_text("{not json", encoding="utf-8")
    assert _load_seat_resources(_SEAT) == ""


@pytest.mark.parametrize(
    ("uri", "loaded"),
    [
        ("file:///abs/doc.md", True),
        ("file:///abs/**/*.md", True),
        ("file://.kiro/steering/**/*.md", False),
        ("file://AGENTS.md", False),
        ("file://~/doc.md", False),
        ("skill:///abs/SKILL.md", False),
        (None, False),
    ],
)
def test_seat_loads_resource(uri, loaded):
    assert seat_loads_resource(uri) is loaded


# --- the unread-steering notice no longer names what now loads -------------------


def _notice(home, backend) -> list[str]:
    seen: list[str] = []
    client = SimpleNamespace(capabilities=capabilities_for(backend), cwd="")
    with patch.object(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **kw: seen.append(text),
    ):
        asyncio.run(
            chat_runner._notice_unread_steering(SimpleNamespace(), SimpleNamespace(), client, _SEAT)
        )
    return seen


@pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
def test_the_notice_leaves_out_absolute_entries_where_they_load(home, guide, backend):
    _write_seat(home, ["file://.kiro/steering/**/*.md", f"file://{guide}"])
    (notice,) = _notice(home, backend)
    assert "(file://.kiro/steering/**/*.md)" in notice


def test_the_notice_still_names_absolute_entries_where_nothing_loads_them(home, guide):
    _write_seat(home, [f"file://{guide}"])
    (notice,) = _notice(home, ACP_BACKEND_OPENCODE)
    assert "(file:///" in notice


def test_no_notice_when_every_entry_is_loaded(home, guide):
    _write_seat(home, [f"file://{guide}"])
    assert _notice(home, ACP_BACKEND_CLAUDE) == []


# --- antigravity: a companion provider label, not an ACP backend ----------------


def test_antigravity_is_loaded_by_crew_but_reads_agents_md_through_crew_too():
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    assert acp_driver.crew_loads_spec_steering(_PROVIDER_LABEL_ANTIGRAVITY)
    # Not shown to read AGENTS.md from its cwd: Crew's load keeps it (a duplicate at worst).
    assert not acp_driver.reads_project_agents_md(_PROVIDER_LABEL_ANTIGRAVITY)
    assert not acp_driver.crew_loads_spec_steering("")
    assert not acp_driver.crew_loads_spec_steering(None)


def test_antigravity_keeps_the_project_agents_md_in_a_seat_load(home, project):
    (project / "AGENTS.md").write_text("AGENTS_MD_RULE\n", encoding="utf-8")
    _write_seat(home, [f"file://{project / 'AGENTS.md'}"])
    assert "AGENTS_MD_RULE" in _part(
        project=str(project), provider_type=_PROVIDER_LABEL_ANTIGRAVITY
    )


def test_antigravity_is_no_longer_read_as_kiro_cli_by_the_folder_dedup(home, project):
    """Under "acp" the project trees counted as delivered natively; agy never gets them."""
    from kiro_crew import context as ctx

    with patch.object(ctx.acp_driver, "inherits_default_resources", return_value=True):
        assert ctx._project_steering_delivered(PROVIDER_ACP, False, str(project), agent=_SEAT)
        assert not ctx._project_steering_delivered(
            _PROVIDER_LABEL_ANTIGRAVITY, False, str(project), agent=_SEAT
        )


def test_antigravity_keeps_the_inherited_skill_trees_it_had_under_the_acp_label():
    """Relabelling must not strip skills: agy has no skill trees of its own to read."""
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    assert acp_driver.lends_inherited_skills(PROVIDER_ACP)
    assert acp_driver.lends_inherited_skills(_PROVIDER_LABEL_ANTIGRAVITY)
    assert not acp_driver.lends_inherited_skills(PROVIDER_CLAUDE_CODE)
    assert not acp_driver.lends_inherited_skills(PROVIDER_LABEL_CODEX)
    assert not acp_driver.lends_inherited_skills(None)
