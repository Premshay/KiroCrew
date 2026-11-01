"""Route registration for the owner's arc board console.

A prefix of its own, deliberately: ``/api/arcs`` is in
``_STRICT_INTERNAL_API_PATHS`` (loopback + internal secret, the members' door),
while these are the owner's browser routes, gated by
``require_owner_dashboard_request`` in the handlers.

Within the slice the literal ``/api/arc-board/arcs`` is registered before the
``/api/arc-board/arcs/{arc_id}`` pattern, for the same reason
``/api/chat/slots/cleanup`` precedes ``/api/chat/slots/{slot}``.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard.handlers import arc_board as arc_board_handlers


def register(app: web.Application) -> None:
    """Register the owner's arc board routes on *app*."""
    app.router.add_get("/api/arc-board/arcs", arc_board_handlers.api_arc_board_list)
    app.router.add_post("/api/arc-board/arcs", arc_board_handlers.api_arc_board_create)
    app.router.add_get("/api/arc-board/arcs/{arc_id}", arc_board_handlers.api_arc_board_detail)
    app.router.add_post(
        "/api/arc-board/arcs/{arc_id}/members",
        arc_board_handlers.api_arc_board_add_member,
    )
    app.router.add_get(
        "/api/arc-board/arcs/{arc_id}/report", arc_board_handlers.api_arc_board_report
    )
    app.router.add_post(
        "/api/arc-board/arcs/{arc_id}/members/remove",
        arc_board_handlers.api_arc_board_remove_member,
    )
    app.router.add_post(
        "/api/arc-board/arcs/{arc_id}/autonomy",
        arc_board_handlers.api_arc_board_autonomy,
    )
    app.router.add_post(
        "/api/arc-board/arcs/{arc_id}/channel", arc_board_handlers.api_arc_board_open_channel
    )
    app.router.add_post("/api/arc-board/arcs/{arc_id}/note", arc_board_handlers.api_arc_board_note)
    app.router.add_post(
        "/api/arc-board/arcs/{arc_id}/answer", arc_board_handlers.api_arc_board_answer
    )
    app.router.add_post(
        "/api/arc-board/arcs/{arc_id}/accept", arc_board_handlers.api_arc_board_accept
    )
