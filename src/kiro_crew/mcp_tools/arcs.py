"""Arc board tools: read the standing brief, read or search the board, write entries.

No tool takes a session key or author. The verified caller identity is the only
coordinate the gateway uses, and it is also the entry's recorded author.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.catalog.arcs import schemas  # noqa: F401 - shared declaration owner


def _strict_session_key() -> tuple[str, str]:
    return mcp_core.require_strict_session_key(
        "Error: this session's identity could not be verified strictly, so "
        "the arc board is not reachable from here."
    )


def _response(payload: dict[str, Any]) -> str:
    err = payload.get("error")
    if err:
        return f"Error: {err}"
    return json.dumps(payload, indent=2)


def _get(path: str) -> str:
    sk, err = _strict_session_key()
    return err if err else _response(mcp_core._get(path, session_key=sk))


def _post(path: str, payload: dict[str, Any]) -> str:
    sk, err = _strict_session_key()
    return err if err else _response(mcp_core._post(path, payload, session_key=sk))


def _base(args: dict[str, Any]) -> str:
    return f"/api/arcs/{quote(str(args['arc_id']), safe='')}"


def arc_brief(name: str, args: dict[str, Any]) -> str:
    return _get(f"{_base(args)}/brief")


def arc_board_read(name: str, args: dict[str, Any]) -> str:
    query = f"?{urlencode({'after': args['after']})}" if args.get("after") else ""
    return _get(f"{_base(args)}/entries{query}")


def arc_board_grep(name: str, args: dict[str, Any]) -> str:
    return _get(f"{_base(args)}/entries?{urlencode({'q': args['query']})}")


def arc_board_write(name: str, args: dict[str, Any]) -> str:
    payload = {key: value for key, value in args.items() if key != "arc_id"}
    return _post(f"{_base(args)}/entries", payload)


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "arc_brief": arc_brief,
    "arc_board_read": arc_board_read,
    "arc_board_grep": arc_board_grep,
    "arc_board_write": arc_board_write,
}
