"""Isolated voice-route application factory for endpoint tests."""

from aiohttp import web


def _make_voice_app(state):
    from kiro_crew.dashboard.chat_voice import (
        api_voice_cancel,
        api_voice_config,
        api_voice_replay,
        api_voice_replay_stream,
        api_voice_synthesize,
        register_voice_lifecycle,
    )

    app = web.Application()
    app["state"] = state
    register_voice_lifecycle(app)
    app.router.add_get("/api/voice/config", api_voice_config)
    app.router.add_put("/api/voice/config", api_voice_config)
    app.router.add_post("/api/voice/synthesize", api_voice_synthesize)
    app.router.add_post("/api/voice/cancel", api_voice_cancel)
    app.router.add_post("/api/voice/replay", api_voice_replay)
    app.router.add_get("/api/voice/replay/{job_id}", api_voice_replay_stream)
    return app
