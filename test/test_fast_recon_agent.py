"""Generated-spec containment for the coordinator's CPU-fast worker."""

from __future__ import annotations

import json


def test_fast_recon_agent_is_model_pinned_and_core_allowlisted(tmp_path, monkeypatch):
    from kiro_crew import agent as agent_mod
    from kiro_crew.agent_files import FAST_RECON_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {
                    "kirocrew-core": {
                        "command": "/bin/kirocrew",
                        "args": ["mcp-core", "--include-tools=spawn_run"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents_dir)

    agent_mod._install_fast_recon_agent()

    config = json.loads((agents_dir / FAST_RECON_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert FAST_RECON_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES
    assert config["name"] == "kirocrew-fast"
    assert config["model"] == "fast"
    assert config["includeMcpJson"] is False
    assert config["tools"] == ["fs_read", "grep", "glob", "@kirocrew-core"]
    assert "--include-tools" not in " ".join(config["mcpServers"]["kirocrew-core"]["args"])

    excluded = set(config["managedToolPolicy"]["exclude"])
    assert {
        "spawn_run",
        "work_item_launch",
        "work_item_revoke_assignment",
        "work_item_assigned_read",
        "work_item_submit_handoff",
    } - excluded == {"work_item_assigned_read", "work_item_submit_handoff"}
