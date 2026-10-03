"""The deny floor must see a command the event did not classify as shell.

The DeepSeek Harness streams its ``bash`` tool as ACP ``kind: "other"``, so the
permission event is built with ``is_shell=False`` and ``shell_command=None``.
The gate then saw only the title, and ``git push --dry-run origin main`` ran
from a DeepSeek seat although the git-publish floor denies it. The command is
still in the raw params; it must reach the deny floor whatever the kind says.

Drives a REAL ``HookManager`` against the default policy, like
``test_hook_deny_classification``: the value under test is chosen inside
``on_tool_call``.
"""

from __future__ import annotations

import pytest

from kiro_crew.hooks import TOOL_DENY, HookManager
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context

PUSH = "git push --dry-run origin main"


@pytest.fixture(autouse=True)
def _default_context():
    from kiro_crew.config.loader import KiroCrewConfig

    ctx_mod.set_context(build_default_context(KiroCrewConfig.load()))
    yield
    ctx_mod.reset_context()


@pytest.mark.parametrize(
    "is_shell, command",
    [
        (True, PUSH),  # a classified shell call: always denied
        (False, None),  # dsh: kind "other", command only in raw params
    ],
)
def test_a_protected_branch_push_is_denied_whatever_the_kind(is_shell, command) -> None:
    result = HookManager().on_tool_call(
        "unknown",
        session_key="subagent:test",
        tool_kind="other",
        raw_params={"command": PUSH, "description": "push"},
        command=command,
        is_shell=is_shell,
    )

    assert result.action == TOOL_DENY
    assert "git-publish-push-protected-branch-name" in (result.reason or "")


def test_a_harmless_raw_command_is_not_denied_by_this_path() -> None:
    """Deny-only: a benign command in raw params must not trip the floor."""
    result = HookManager().on_tool_call(
        "unknown",
        session_key="subagent:test",
        tool_kind="other",
        raw_params={"command": "git push origin feature-x"},
        is_shell=False,
    )

    assert result.action != TOOL_DENY
