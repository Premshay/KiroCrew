"""Guarded Code Review Sage bridge."""

from __future__ import annotations

import json
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.catalog.apps import advertised as advertised
from kiro_crew.mcp_tools.catalog.apps import app_enabled_state as app_enabled_state
from kiro_crew.mcp_tools.catalog.apps import schemas as schemas
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import code_review_sage_call_allowed, sanitize_json_values

__all__ = ["advertised", "app_enabled_state", "schemas"]


def _redact_json_strings(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {_redact_json_strings(k): _redact_json_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json_strings(item) for item in value]
    return value


def code_review_sage_api(name: str, args: dict[str, Any]) -> str:
    _crs_method = args["method"]
    _crs_path = args["path"]
    if not code_review_sage_call_allowed(_crs_method, _crs_path):
        return (
            f"Error: {_crs_method} {_crs_path} is not part of the "
            "code-review-sage agent surface."
        )
    _crs_body: dict[str, Any] = {}
    _crs_body_raw = args.get("body_json") or ""
    if _crs_body_raw:
        try:
            _crs_parsed = json.loads(_crs_body_raw)
        except ValueError:
            return "Error: body_json is not valid JSON."
        if not isinstance(_crs_parsed, dict):
            return "Error: body_json must encode a JSON object."
        # Same reason as the ops bridge: schema sanitization saw body_json as one
        # opaque string, so walk the DECODED structure and redact on the way in.
        _crs_body = _redact_json_strings(sanitize_json_values(_crs_parsed))
    _crs_query = args.get("query") or ""
    _crs_url = "/api/apps/code-review-sage" + _crs_path
    if _crs_query:
        _crs_url += "?" + _crs_query
    _crs_resp = (
        mcp_core._get(_crs_url) if _crs_method == "GET" else mcp_core._post(_crs_url, _crs_body)
    )
    # Redact BEFORE truncating: slicing first can cut a credential in half at the
    # cap, leaving a fragment outside the redaction pattern's matches.
    _crs_text = redact(json.dumps(_crs_resp, ensure_ascii=False, default=str))
    _crs_cap = 60_000
    if len(_crs_text) > _crs_cap:
        _crs_text = (
            _crs_text[:_crs_cap] + f"\n… truncated ({len(_crs_text)} chars total). Read one run "
            "(/runs/<run_id>) to see the rest."
        )
    return _crs_text


HANDLERS = {"code_review_sage_api": code_review_sage_api}
