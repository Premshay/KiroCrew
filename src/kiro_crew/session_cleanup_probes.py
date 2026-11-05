"""Default probes for cleanup managers without gateway work tracking."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider
    from kiro_crew.session_cleanup import SessionEntry


def _no_attached_subagents(key: str) -> bool:
    """Default sub-agent probe: a manager with no dashboard has no children."""
    return False


def _no_pending_injection(key: str) -> bool:
    """Default injection probe: a manager with no gateway injects nothing."""
    return False


def _no_background_launch(provider: LLMProvider) -> tuple[float, str] | None:
    """Default background-launch probe: no provider reports a launch."""
    return None


ActivePidCollector: TypeAlias = Callable[
    [MutableMapping[str, "SessionEntry"]],
    tuple[set[int], bool],
]
PeriodicPidSweep = Callable[[int, set[int]], tuple[set[str], list[int]]]
PidWriteback = Callable[[int, list[int], set[str]], int]
