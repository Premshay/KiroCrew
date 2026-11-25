"""The label an HTTP route of the gateway can carry: an action name and a tier.

A :class:`RouteAccess` says what a route does (for example ``widgets.reset``) and how long its
request and response shape is promised. :data:`ROUTES` records the labels, keyed by
method and path pattern.

The table is separate from route registration on purpose. One handler can serve two
routes, so a label on the handler would be ambiguous, and registration runs on the
gateway boot path, where a label must cost nothing.

No request reads this module: it decides nothing about who may call a route.
``test/test_route_access_labels.py`` holds the registered routes against it.
Rules for picking a name and a tier: ``docs/system-specs/modules/route-access.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ACTION_NAME_RE",
    "PREVIEW",
    "ROUTES",
    "STABLE",
    "TIERS",
    "UNSTABLE",
    "RouteAccess",
]

# Request and response shapes only gain fields.
STABLE: Final = "stable"
# The shape is nearly settled; small changes are possible.
PREVIEW: Final = "preview"
# No promise: the shape changes whenever the dashboard needs it to.
UNSTABLE: Final = "unstable"

TIERS: Final = frozenset({STABLE, PREVIEW, UNSTABLE})

# ``<area>.<verb>``: lower-case parts joined by dots, at least two of them.
ACTION_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


@dataclass(frozen=True)
class RouteAccess:
    """One route's action name and stability tier."""

    action: str
    tier: str


# Keyed by (METHOD, path pattern). The pattern is the route's canonical path: the path
# as registered, with any regex dropped from a ``{name:regex}`` segment. A HEAD route
# registered beside a GET route of the same path takes the GET route's entry.
# Who may call a route is decided by its handler; the comment beside an entry only
# records it.
ROUTES: Final[dict[tuple[str, str], RouteAccess]] = {
    ("GET", "/api/agents/{name}/models"): RouteAccess("agents.models.list", UNSTABLE),
    ("GET", "/api/apps/auto-improvement/environment/check"): RouteAccess(
        "auto_improvement.environment.status", UNSTABLE
    ),
    ("POST", "/api/apps/auto-improvement/environment/check"): RouteAccess(
        "auto_improvement.environment.check", UNSTABLE
    ),
    ("GET", "/api/apps/auto-improvement/repository/assessment"): RouteAccess(
        "auto_improvement.assessment.read", UNSTABLE
    ),
    ("POST", "/api/apps/auto-improvement/repository/assessment"): RouteAccess(
        "auto_improvement.assessment.start", UNSTABLE
    ),
    ("POST", "/api/apps/auto-improvement/repository/assessment/stop"): RouteAccess(
        "auto_improvement.assessment.stop", UNSTABLE
    ),
    ("GET", "/api/apps/auto-improvement/repository/survey"): RouteAccess(
        "auto_improvement.repository.survey", UNSTABLE
    ),
    ("DELETE", "/api/apps/code-review-sage/learnings/candidates"): RouteAccess(
        "code_review_sage.candidates.clear", UNSTABLE
    ),
    ("GET", "/api/apps/code-review-sage/learnings/consolidation-previews"): RouteAccess(
        "code_review_sage.consolidation.list", UNSTABLE
    ),
    ("POST", "/api/apps/code-review-sage/learnings/consolidation-previews/apply"): RouteAccess(
        "code_review_sage.consolidation.apply", UNSTABLE
    ),
    (
        "GET",
        "/api/apps/code-review-sage/learnings/consolidation-previews/{preview_id}",
    ): RouteAccess("code_review_sage.consolidation.read", UNSTABLE),
    ("POST", "/api/apps/code-review-sage/learnings/rules/{action}"): RouteAccess(
        "code_review_sage.rules.change", UNSTABLE
    ),
    ("GET", "/api/apps/design-critique/contexts"): RouteAccess(
        "design_critique.contexts.list", UNSTABLE
    ),
    ("POST", "/api/apps/design-critique/contexts"): RouteAccess(
        "design_critique.contexts.create", UNSTABLE
    ),
    ("DELETE", "/api/apps/design-critique/contexts/{context_id}"): RouteAccess(
        "design_critique.contexts.delete", UNSTABLE
    ),
    ("PATCH", "/api/apps/design-critique/contexts/{context_id}"): RouteAccess(
        "design_critique.contexts.update", UNSTABLE
    ),
    ("GET", "/api/apps/design-critique/design-rounds"): RouteAccess(
        "design_critique.rounds.list", UNSTABLE
    ),
    ("POST", "/api/apps/design-critique/design-rounds"): RouteAccess(
        "design_critique.rounds.create", UNSTABLE
    ),
    ("PATCH", "/api/apps/design-critique/design-rounds/{round_id}"): RouteAccess(
        "design_critique.rounds.update", UNSTABLE
    ),
    ("GET", "/api/apps/design-critique/runs"): RouteAccess("design_critique.runs.list", UNSTABLE),
    ("POST", "/api/apps/design-critique/runs"): RouteAccess(
        "design_critique.runs.create", UNSTABLE
    ),
    ("PATCH", "/api/apps/design-critique/runs/{run_id}"): RouteAccess(
        "design_critique.runs.update", UNSTABLE
    ),
    ("GET", "/api/arc-board/arcs"): RouteAccess("arc_board.list", UNSTABLE),
    ("POST", "/api/arc-board/arcs"): RouteAccess("arc_board.create", UNSTABLE),
    ("GET", "/api/arc-board/arcs/{arc_id}"): RouteAccess("arc_board.read", UNSTABLE),
    ("POST", "/api/arc-board/arcs/{arc_id}/accept"): RouteAccess("arc_board.accept", UNSTABLE),
    ("POST", "/api/arc-board/arcs/{arc_id}/answer"): RouteAccess("arc_board.answer", UNSTABLE),
    ("POST", "/api/arc-board/arcs/{arc_id}/autonomy"): RouteAccess(
        "arc_board.autonomy.change", UNSTABLE
    ),
    ("POST", "/api/arc-board/arcs/{arc_id}/channel"): RouteAccess(
        "arc_board.channel.link", UNSTABLE
    ),
    ("POST", "/api/arc-board/arcs/{arc_id}/close"): RouteAccess("arc_board.close", UNSTABLE),
    ("POST", "/api/arc-board/arcs/{arc_id}/members"): RouteAccess(
        "arc_board.members.add", UNSTABLE
    ),
    ("POST", "/api/arc-board/arcs/{arc_id}/members/remove"): RouteAccess(
        "arc_board.members.remove", UNSTABLE
    ),
    ("POST", "/api/arc-board/arcs/{arc_id}/note"): RouteAccess("arc_board.notes.add", UNSTABLE),
    ("GET", "/api/arc-board/arcs/{arc_id}/report"): RouteAccess("arc_board.report.read", UNSTABLE),
    ("POST", "/api/arc-board/arcs/{arc_id}/settings"): RouteAccess(
        "arc_board.settings.change", UNSTABLE
    ),
    ("GET", "/api/arcs/mine"): RouteAccess("arcs.list_mine", UNSTABLE),
    ("GET", "/api/arcs/{arc_id}/brief"): RouteAccess("arcs.brief.read", UNSTABLE),
    ("POST", "/api/arcs/{arc_id}/deliver/{entry_id}"): RouteAccess(
        "arcs.entries.deliver", UNSTABLE
    ),
    ("GET", "/api/arcs/{arc_id}/entries"): RouteAccess("arcs.entries.list", UNSTABLE),
    ("POST", "/api/arcs/{arc_id}/entries"): RouteAccess("arcs.entries.write", UNSTABLE),
    ("POST", "/api/channels/{id}/attach-requests/{rid}"): RouteAccess(
        "channels.attach_requests.resolve", UNSTABLE
    ),
    ("POST", "/api/channels/{id}/sessions"): RouteAccess("channels.sessions.attach", UNSTABLE),
    ("GET", "/api/chat/slots/{slot}/account"): RouteAccess("chat.account.read", UNSTABLE),
    ("POST", "/api/chat/slots/{slot}/account"): RouteAccess("chat.account.change", UNSTABLE),
    ("POST", "/api/chat/slots/{slot}/return-handoff"): RouteAccess("chat.handoff.return", UNSTABLE),
    ("GET", "/api/chat/slots/{slot}/runtime"): RouteAccess("chat.runtime.read", UNSTABLE),
    ("POST", "/api/chat/slots/{slot}/runtime"): RouteAccess("chat.runtime.change", UNSTABLE),
    ("POST", "/api/decisions/preference"): RouteAccess("decisions.preference.record", UNSTABLE),
    ("POST", "/api/decisions/preference/feedback"): RouteAccess(
        "decisions.preference.feedback", UNSTABLE
    ),
    ("POST", "/api/preference-consult"): RouteAccess("preference.consult", UNSTABLE),
    ("POST", "/api/session-channel"): RouteAccess("sessions.channel.manage", UNSTABLE),
    ("POST", "/api/session-checkpoint"): RouteAccess("sessions.checkpoint.write", UNSTABLE),
    ("POST", "/api/session-maintenance"): RouteAccess("sessions.maintenance.acknowledge", UNSTABLE),
    ("POST", "/api/session-restart-continuation"): RouteAccess(
        "sessions.restart_continuation.arm", UNSTABLE
    ),
    ("GET", "/api/sessions/restart-blockers"): RouteAccess(
        "sessions.restart_blockers.list", UNSTABLE
    ),
    ("POST", "/api/sessions/restart-blockers/clear"): RouteAccess(
        "sessions.restart_blockers.clear", UNSTABLE
    ),
    ("POST", "/api/voice/replay"): RouteAccess("voice.replay.start", UNSTABLE),
    ("GET", "/api/voice/replay/{job_id}"): RouteAccess("voice.replay.read", UNSTABLE),
    ("GET", "/api/work-items"): RouteAccess("work_items.list", UNSTABLE),
    ("GET", "/api/work-items/archive"): RouteAccess("work_items.archive.list", UNSTABLE),
    ("GET", "/api/work-items/archive/{cycle_id}"): RouteAccess("work_items.archive.read", UNSTABLE),
    ("GET", "/api/work-items/assigned"): RouteAccess("work_items.assigned.list", UNSTABLE),
    ("GET", "/api/work-items/assigned/{item_id}"): RouteAccess(
        "work_items.assigned.read", UNSTABLE
    ),
    ("POST", "/api/work-items/assigned/{item_id}/handoff"): RouteAccess(
        "work_items.assigned.handoff", UNSTABLE
    ),
    ("POST", "/api/work-items/assigned/{item_id}/progress"): RouteAccess(
        "work_items.assigned.progress", UNSTABLE
    ),
    ("POST", "/api/work-items/cycle/close"): RouteAccess("work_items.cycle.close", UNSTABLE),
    ("POST", "/api/work-items/cycle/open"): RouteAccess("work_items.cycle.open", UNSTABLE),
    ("POST", "/api/work-items/items"): RouteAccess("work_items.items.create", UNSTABLE),
    ("POST", "/api/work-items/items/evaluate"): RouteAccess("work_items.items.evaluate", UNSTABLE),
    ("GET", "/api/work-items/items/{item_id}"): RouteAccess("work_items.items.read", UNSTABLE),
    ("POST", "/api/work-items/items/{item_id}/dispatch-retry"): RouteAccess(
        "work_items.items.retry_dispatch", UNSTABLE
    ),
    ("POST", "/api/work-items/items/{item_id}/launch"): RouteAccess(
        "work_items.items.launch", UNSTABLE
    ),
    ("POST", "/api/work-items/items/{item_id}/revoke-assignment"): RouteAccess(
        "work_items.items.revoke_assignment", UNSTABLE
    ),
    ("POST", "/api/work-items/items/{item_id}/transition"): RouteAccess(
        "work_items.items.transition", UNSTABLE
    ),
    ("POST", "/api/work-items/items/{item_id}/update"): RouteAccess(
        "work_items.items.update", UNSTABLE
    ),
    ("GET", "/api/work-items/launch/candidates"): RouteAccess(
        "work_items.launch.candidates", UNSTABLE
    ),
    # App token, owner dashboard or internal caller may read (apps/browser_sites_routes.py).
    ("GET", "/api/apps/{name}/browser-sites"): RouteAccess("apps.browser_sites.read", UNSTABLE),
    # Owner dashboard only: 403 for an app token and an internal caller.
    ("POST", "/api/apps/{name}/browser-sites"): RouteAccess("apps.browser_sites.change", UNSTABLE),
    # Owner gate (handlers/connections_app_manifest.py); writes nothing.
    ("GET", "/api/connections/app-manifests"): RouteAccess(
        "connections.app_manifests.read", UNSTABLE
    ),
    # Owner dashboard only (chat_api/slot_effort.py); restarts the slot's idle session.
    ("POST", "/api/chat/slots/{slot}/approve-for-me"): RouteAccess(
        "chat.approve_for_me.change", UNSTABLE
    ),
    # Dashboard user only (chat_folders._toggle_slot_user_flag): 403 for an app token
    # and an internal caller.
    ("PATCH", "/api/chat/slots/{slot}/source-links-off"): RouteAccess(
        "chat.source_links_off.change", UNSTABLE
    ),
    # Owner gate, then the machine owner alone (crew_api_grant_requests). The Crew API
    # RFC marks the Owner tier experimental. Contract test: test_crew_api_grant_requests.py.
    ("GET", "/v1/crew/owner/grants"): RouteAccess("crew_api.grants.list", PREVIEW),
    # Owner gate (crew_api_owner.owner_route); Crew API phase 1a, no preview marking.
    # Contract test: test_crew_api_models.py.
    ("GET", "/v1/models"): RouteAccess("crew_api.models.list", STABLE),
    # Owner gate, then the machine owner alone (crew_api_responses).
    # Contract test: test_crew_api_responses.py.
    ("POST", "/v1/responses"): RouteAccess("crew_api.responses.create", PREVIEW),
}
