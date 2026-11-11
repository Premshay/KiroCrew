"""Owner CLI channel adoption and atomic settings validation."""

from __future__ import annotations

import argparse

import pytest

from kiro_crew import arcs, cli_commands_arc, cli_parser_agent


@pytest.fixture()
def arc(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    made = arcs.create_arc({"title": "Pilot", "repo": "o/r"})
    arcs.add_member(made["id"], "chat-builder", "Builder")
    return arcs.set_channel(made["id"], "11111111", owned=True)


def settings(arc_id, *flags):
    parser = argparse.ArgumentParser()
    cli_parser_agent.register_arc_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["arc", "settings", arc_id, *flags])


def test_channel_flag_adopts_shared_channel_without_changing_other_settings(arc, capsys):
    cli_commands_arc._arc(settings(arc["id"], "--channel", "22222222"))
    updated = arcs.read_arc(arc["id"])
    assert updated == {**arc, "channel_id": "22222222", "channel_owned": False}
    assert "channel=22222222" in capsys.readouterr().out


def test_combined_settings_are_saved_together(arc):
    cli_commands_arc._arc(
        settings(
            arc["id"],
            "--repo",
            "new/repo",
            "--coordinator",
            "chat-builder",
            "--channel",
            "33333333",
        )
    )
    updated = arcs.read_arc(arc["id"])
    assert updated["repo"] == "new/repo"
    assert updated["coordinator"] == "chat-builder"
    assert updated["channel_id"] == "33333333"
    assert updated["channel_owned"] is False


@pytest.mark.parametrize("channel", ["", "   "])
def test_invalid_channel_preserves_all_stored_settings(arc, channel):
    with pytest.raises(SystemExit) as error:
        cli_commands_arc._arc(
            settings(
                arc["id"],
                "--repo",
                "new/repo",
                "--coordinator",
                "chat-builder",
                "--channel",
                channel,
            )
        )
    assert error.value.code == 1
    assert arcs.read_arc(arc["id"]) == arc


def test_reselecting_owned_channel_preserves_ownership(arc):
    cli_commands_arc._arc(settings(arc["id"], "--channel", "11111111"))
    assert arcs.read_arc(arc["id"]) == arc


def test_repo_only_edit_preserves_channel_and_ownership(arc):
    cli_commands_arc._arc(settings(arc["id"], "--repo", "new/repo"))
    assert arcs.read_arc(arc["id"]) == {**arc, "repo": "new/repo"}


def test_invalid_coordinator_does_not_retarget_channel(arc):
    with pytest.raises(arcs.ArcError, match="must be a member"):
        arcs.update_settings(arc["id"], channel="22222222", coordinator="not-a-member")
    assert arcs.read_arc(arc["id"]) == arc


def test_settings_requires_at_least_one_flag(arc):
    with pytest.raises(SystemExit) as error:
        cli_commands_arc._arc(settings(arc["id"]))
    assert error.value.code == 1
    assert arcs.read_arc(arc["id"]) == arc
