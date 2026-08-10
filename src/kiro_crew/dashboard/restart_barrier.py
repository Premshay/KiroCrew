"""In-memory acknowledgement barrier for a coordinated session reset."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


@dataclass
class RestartBarrier:
    """Require fresh checkpoints and acknowledgements before a session reset.

    The barrier is intentionally process-local: it protects the interval before
    a reset, and a completed reset discards its state.  Callers refresh the set
    of busy sessions immediately before every destructive lifecycle action so
    newly started work cannot slip through an earlier acknowledgement.
    """

    identifier: str = ""
    opened_at: str = ""
    checkpoint_generation: dict[str, int] = field(default_factory=dict)
    checkpoint_baseline: dict[str, int] = field(default_factory=dict)
    acknowledged_generation: dict[str, int] = field(default_factory=dict)
    activity_markers: dict[str, float] = field(default_factory=dict)
    acknowledged_activity: dict[str, float] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    unmanaged_busy: set[str] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return bool(self.identifier)

    def open(self, managed_busy: dict[str, float], unmanaged_busy: set[str]) -> None:
        """Open a new barrier and snapshot the checkpoint baseline."""
        self.identifier = uuid4().hex
        self.opened_at = datetime.now(timezone.utc).isoformat()
        self.checkpoint_baseline = {
            key: self.checkpoint_generation.get(key, 0) for key in managed_busy
        }
        self.acknowledged_generation = {}
        self.activity_markers = dict(managed_busy)
        self.acknowledged_activity = {}
        self.required = set(managed_busy)
        self.unmanaged_busy = set(unmanaged_busy)

    def refresh(self, managed_busy: dict[str, float], unmanaged_busy: set[str]) -> None:
        """Refresh the live work set without accepting stale acknowledgements."""
        if not self.active:
            return
        self.required = set(managed_busy)
        self.unmanaged_busy = set(unmanaged_busy)
        self.activity_markers = dict(managed_busy)
        for key in managed_busy:
            self.checkpoint_baseline.setdefault(key, self.checkpoint_generation.get(key, 0))

    def note_checkpoint(self, session_key: str) -> None:
        """Record a checkpoint that can satisfy a subsequently-open barrier."""
        self.checkpoint_generation[session_key] = self.checkpoint_generation.get(session_key, 0) + 1

    def acknowledge(self, session_key: str) -> tuple[bool, str]:
        """Acknowledge only after a checkpoint written since this barrier opened."""
        if not self.active:
            return False, "no restart acknowledgement barrier is active"
        if session_key not in self.required:
            return False, "this session is not currently required to acknowledge the reset"
        current = self.checkpoint_generation.get(session_key, 0)
        baseline = self.checkpoint_baseline.get(session_key, current)
        if current <= baseline:
            return False, "record a fresh session_checkpoint before acknowledging the reset"
        self.acknowledged_generation[session_key] = current
        self.acknowledged_activity[session_key] = self.activity_markers[session_key]
        return True, "acknowledgement recorded"

    def pending(self) -> list[str]:
        return sorted(
            key
            for key in self.required
            if self.acknowledged_generation.get(key, 0)
            <= self.checkpoint_baseline.get(key, self.checkpoint_generation.get(key, 0))
            or self.acknowledged_activity.get(key) != self.activity_markers.get(key)
        )

    def ready(self) -> bool:
        return self.active and not self.pending() and not self.unmanaged_busy

    def clear(self) -> None:
        self.identifier = ""
        self.opened_at = ""
        self.checkpoint_baseline = {}
        self.acknowledged_generation = {}
        self.activity_markers = {}
        self.acknowledged_activity = {}
        self.required = set()
        self.unmanaged_busy = set()

    def payload(self) -> dict[str, object]:
        if not self.active:
            return {
                "active": False,
                "ready": True,
                "required": [],
                "pending": [],
                "unmanaged_busy": [],
            }
        pending = self.pending()
        return {
            "active": True,
            "id": self.identifier,
            "opened_at": self.opened_at,
            "ready": not pending and not self.unmanaged_busy,
            "required": sorted(self.required),
            "pending": pending,
            "unmanaged_busy": sorted(self.unmanaged_busy),
        }
