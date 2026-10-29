"""Gateway and app lifecycle signals cancel repository assessment work."""

import threading

import pytest
from aiohttp import web

from kiro_crew.apps import teardown

from ..backend import repository_assessment, routes, store


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["shutdown", "disable"])
async def test_lifecycle_signals_stop_assessment(event, monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(repository_assessment, "_stop", stop)
    app = web.Application()
    routes.register_routes(app)
    try:
        if event == "shutdown":
            app.freeze()
            await app.on_cleanup.send(app)
        else:
            await teardown.notify_app_disabled(store.APP_NAME)
        assert stop.is_set()
    finally:
        teardown.unregister_app_disable_hook(store.APP_NAME)
