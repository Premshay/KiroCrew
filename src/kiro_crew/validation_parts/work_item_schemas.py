"""Conductor work-item mutation and worker reporting schemas."""

from __future__ import annotations

from typing import Any

from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError

WORK_CYCLE_OPEN_SCHEMA = ToolSchema(
    tool_name="work_cycle_open",
    fields=[
        FieldSpec("goal", str, required=True, max_len=2000),
        FieldSpec("next_action", str, required=True, max_len=2000),
    ],
)


WORK_ITEM_CREATE_SCHEMA = ToolSchema(
    tool_name="work_item_create",
    fields=[
        FieldSpec("title", str, required=True, max_len=240),
        FieldSpec("acceptance", dict, required=True),
        FieldSpec("canonical_ref", str, max_len=2000),
        FieldSpec("declared_resources", list, max_items=32, item_type=str, item_max_len=512),
        FieldSpec("next_action", str, required=True, max_len=2000),
    ],
)


def _work_item_update_fields(cleaned: dict[str, Any]) -> None:
    if not any(
        name in cleaned for name in ("canonical_ref", "declared_resources", "next_action", "event")
    ):
        raise ValidationError("args", "supply at least one mutable work-item field")
    if "event_kind" in cleaned and "event" not in cleaned:
        raise ValidationError("event_kind", "requires event")


_WORK_ITEM_UPDATE_FIELDS = [
    FieldSpec("canonical_ref", str, max_len=2000),
    FieldSpec("declared_resources", list, max_items=32, item_type=str, item_max_len=512),
    FieldSpec("next_action", str, max_len=2000),
    FieldSpec("event", str, max_len=2000),
    FieldSpec("event_kind", str, allowed=frozenset({"progress", "blocker"})),
]


WORK_ITEM_UPDATE_SCHEMA = ToolSchema(
    tool_name="work_item_update",
    fields=[
        FieldSpec("item_id", str, required=True, max_len=64),
        *_WORK_ITEM_UPDATE_FIELDS,
    ],
    custom_validator=_work_item_update_fields,
)


WORK_ITEM_UPDATE_ROUTE_SCHEMA = ToolSchema(
    tool_name="work_item_update_route",
    fields=_WORK_ITEM_UPDATE_FIELDS,
    custom_validator=_work_item_update_fields,
)


_WORK_ITEM_TRANSITION_FIELDS = [
    FieldSpec(
        "state",
        str,
        required=True,
        allowed=frozenset({"proposed", "waiting", "rejected", "cancelled"}),
    ),
    FieldSpec("event", str, required=True, max_len=2000),
    FieldSpec("next_action", str, max_len=2000),
]


WORK_ITEM_TRANSITION_SCHEMA = ToolSchema(
    tool_name="work_item_transition",
    fields=[FieldSpec("item_id", str, required=True, max_len=64), *_WORK_ITEM_TRANSITION_FIELDS],
)


WORK_ITEM_TRANSITION_ROUTE_SCHEMA = ToolSchema(
    tool_name="work_item_transition_route", fields=_WORK_ITEM_TRANSITION_FIELDS
)


WORK_ITEM_EVALUATE_SCHEMA = ToolSchema(
    tool_name="work_item_evaluate",
    fields=[
        FieldSpec("item_ids", list, required=True, max_items=64, item_type=str, item_max_len=64)
    ],
)


WORK_CYCLE_CLOSE_SCHEMA = ToolSchema(
    tool_name="work_cycle_close",
    fields=[FieldSpec("summary", str, required=True, max_len=2000)],
)


WORK_ITEM_LIST_SCHEMA = ToolSchema(tool_name="work_item_list")


WORK_ITEM_READ_SCHEMA = ToolSchema(
    tool_name="work_item_read", fields=[FieldSpec("item_id", str, required=True, max_len=64)]
)


WORK_CYCLE_ARCHIVE_LIST_SCHEMA = ToolSchema(tool_name="work_cycle_archive_list")


WORK_CYCLE_ARCHIVE_READ_SCHEMA = ToolSchema(
    tool_name="work_cycle_archive_read",
    fields=[FieldSpec("cycle_id", str, required=True, max_len=64)],
)


_WORK_ITEM_PROGRESS_KINDS = frozenset({"progress", "blocker"})


WORK_ITEM_LAUNCH_CANDIDATES_SCHEMA = ToolSchema(tool_name="work_item_launch_candidates")


WORK_ITEM_LAUNCH_ROUTE_SCHEMA = ToolSchema(
    tool_name="work_item_launch_route",
    fields=[FieldSpec("candidate_id", str, required=True, max_len=64)],
)


WORK_ITEM_LAUNCH_SCHEMA = ToolSchema(
    tool_name="work_item_launch",
    fields=[
        FieldSpec("item_id", str, required=True, max_len=64),
        FieldSpec("candidate_id", str, required=True, max_len=64),
    ],
)


WORK_ITEM_DISPATCH_RETRY_ROUTE_SCHEMA = ToolSchema(tool_name="work_item_dispatch_retry_route")


WORK_ITEM_DISPATCH_RETRY_SCHEMA = ToolSchema(
    tool_name="work_item_dispatch_retry",
    fields=[FieldSpec("item_id", str, required=True, max_len=64)],
)


WORK_ITEM_REVOKE_ROUTE_SCHEMA = ToolSchema(tool_name="work_item_revoke_route")


WORK_ITEM_REVOKE_ASSIGNMENT_SCHEMA = ToolSchema(
    tool_name="work_item_revoke_assignment",
    fields=[FieldSpec("item_id", str, required=True, max_len=64)],
)


WORK_ITEM_ASSIGNED_LIST_SCHEMA = ToolSchema(tool_name="work_item_assigned_list")


WORK_ITEM_ASSIGNED_READ_SCHEMA = ToolSchema(
    tool_name="work_item_assigned_read",
    fields=[FieldSpec("item_id", str, required=True, max_len=64)],
)


_WORK_ITEM_PROGRESS_FIELDS = [
    FieldSpec("text", str, required=True, max_len=2000),
    FieldSpec("kind", str, required=True, allowed=_WORK_ITEM_PROGRESS_KINDS),
]


WORK_ITEM_ASSIGNED_PROGRESS_ROUTE_SCHEMA = ToolSchema(
    tool_name="work_item_assigned_progress_route", fields=_WORK_ITEM_PROGRESS_FIELDS
)


WORK_ITEM_REPORT_PROGRESS_SCHEMA = ToolSchema(
    tool_name="work_item_report_progress",
    fields=[FieldSpec("item_id", str, required=True, max_len=64), *_WORK_ITEM_PROGRESS_FIELDS],
)


_WORK_ITEM_HANDOFF_FIELDS = [
    FieldSpec("outcome", str, required=True, max_len=2000),
    FieldSpec("next_action", str, required=True, max_len=2000),
    FieldSpec(
        "verification",
        list,
        required=True,
        max_items=8,
        item_type=str,
        item_max_len=512,
    ),
    FieldSpec("canonical_ref", str, max_len=2000),
    FieldSpec("blocker", str, max_len=2000),
    FieldSpec("release_condition", str, max_len=2000),
]


def _work_item_handoff_fields(cleaned: dict[str, Any]) -> None:
    if bool(cleaned.get("blocker")) != bool(cleaned.get("release_condition")):
        raise ValidationError(
            "blocker", "blocker and release_condition must both be set or both be empty"
        )


WORK_ITEM_ASSIGNED_HANDOFF_ROUTE_SCHEMA = ToolSchema(
    tool_name="work_item_assigned_handoff_route",
    fields=_WORK_ITEM_HANDOFF_FIELDS,
    custom_validator=_work_item_handoff_fields,
)


WORK_ITEM_SUBMIT_HANDOFF_SCHEMA = ToolSchema(
    tool_name="work_item_submit_handoff",
    fields=[FieldSpec("item_id", str, required=True, max_len=64), *_WORK_ITEM_HANDOFF_FIELDS],
    custom_validator=_work_item_handoff_fields,
)
