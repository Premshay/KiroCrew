"""Record arcs' merged PRs on their boards, every few minutes, from the gateway.

Each pass runs :func:`kiro_crew.arc_github.sync_all` in a thread: it lists the
merged PRs tagged ``Arc: <id>`` for every arc that names a repo, and records the
new ones as DONE entries (see that module). Arcs without a repo cost one store
read. A failing pass is logged and the next one runs on schedule.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Seconds between passes. Merges are rare; ten minutes keeps the board current
#: well inside a working session at a handful of ``gh`` calls an hour.
ARC_GITHUB_SYNC_INTERVAL_S = 600.0
#: First pass after the bind: off the boot path, after the listener is up.
_FIRST_PASS_DELAY_S = 60.0


async def _pass() -> dict[str, int]:
    from kiro_crew import arc_github

    return await asyncio.to_thread(arc_github.sync_all)


async def _loop(interval: float = ARC_GITHUB_SYNC_INTERVAL_S) -> None:
    await asyncio.sleep(_FIRST_PASS_DELAY_S)
    while True:
        try:
            counts = await _pass()
            recorded = sum(c for c in counts.values() if c > 0)
            failed = [arc_id for arc_id, c in counts.items() if c < 0]
            if recorded:
                logger.info("arc github sync recorded %d merged PR(s)", recorded)
            if failed:
                logger.warning("arc github sync failed for %s", ", ".join(failed))
        except Exception:  # noqa: BLE001 -- the next pass runs on schedule
            logger.warning("arc github sync pass failed", exc_info=True)
        await asyncio.sleep(interval)


def kick_arc_github_sync(state: Any) -> None:
    """Start the periodic sync as a tracked background task of the gateway."""
    task = asyncio.create_task(_loop(), name="arc-github-sync")
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
