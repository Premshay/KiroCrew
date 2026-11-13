"""Validation for the guarded Code Review Sage bridge."""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError

CODE_REVIEW_SAGE_ALLOWED_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/review"),
        ("GET", "/runs"),
    }
)


_CRS_RUN_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"


_CRS_DYNAMIC_CALLS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(rf"^/runs/{_CRS_RUN_ID}$")),
    ("GET", re.compile(rf"^/runs/{_CRS_RUN_ID}/report$")),
)


_CRS_API_METHODS = frozenset({"GET", "POST"})


_CRS_API_MAX_PATH = 512


_CRS_QUERY_RE = re.compile(r"^[A-Za-z0-9_.=&%+,:-]*$")


_CRS_MAX_BODY = 32_768


def code_review_sage_call_allowed(method: object, path: object) -> bool:
    """Whether *method* *path* is part of the Sage agent surface.

    Both the tool schema and the handler ask this, so an admitted call cannot
    mean one thing at the boundary and another at the request.
    """
    if not isinstance(method, str) or not isinstance(path, str):
        return False
    if (method, path) in CODE_REVIEW_SAGE_ALLOWED_CALLS:
        return True
    return any(
        method == allowed_method and rx.match(path) for allowed_method, rx in _CRS_DYNAMIC_CALLS
    )


def _validate_crs_api(cleaned: dict[str, Any]) -> None:
    method = cleaned.get("method")
    path = cleaned.get("path")
    if not code_review_sage_call_allowed(method, path):
        raise ValidationError("path", f"{method} {path} is not part of the agent surface")
    if cleaned.get("query") and method != "GET":
        raise ValidationError("query", "query is only accepted on GET calls")
    if cleaned.get("body_json") and method == "GET":
        raise ValidationError("body_json", "GET calls take no body")


CODE_REVIEW_SAGE_API_SCHEMA = ToolSchema(
    tool_name="code_review_sage_api",
    fields=[
        FieldSpec("method", str, required=True, allowed=_CRS_API_METHODS),
        FieldSpec("path", str, required=True, max_len=_CRS_API_MAX_PATH),
        FieldSpec("query", str, max_len=512, pattern=_CRS_QUERY_RE, default=""),
        FieldSpec("body_json", str, max_len=_CRS_MAX_BODY, default=""),
    ],
    custom_validator=_validate_crs_api,
)
