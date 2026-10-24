"""Dashboard requests that exercise the configured-owner admission gate."""

from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import make_mocked_request


def owner_request(method: str, path: str, **kwargs) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="operator")
    request = make_mocked_request(method, path, app=app, **kwargs)
    request["user"] = "operator"
    request["app"] = ""
    request["is_dashboard_user"] = True
    return request
