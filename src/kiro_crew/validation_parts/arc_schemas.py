"""Arc board tool and route schemas.

Identity is never a field: the author of a write is the verified caller, so a
body carrying ``author`` or ``session_key`` is refused as an unknown field.
"""

from __future__ import annotations

import re

from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema

_ARC_ID = re.compile(r"arc_[0-9a-f]{32}")
_ENTRY_KINDS = frozenset({"CLAIM", "FACT", "FAIL", "ASK", "DONE"})

_ENTRY_FIELDS = [
    FieldSpec("kind", str, required=True, allowed=_ENTRY_KINDS),
    FieldSpec("text", str, required=True, max_len=300),
    FieldSpec("step", str, max_len=100),
    FieldSpec("checked", list, max_items=8, item_type=str, item_max_len=200),
    FieldSpec("to", str, max_len=512),
    FieldSpec("reply_to", str, max_len=64),
    FieldSpec("link", str, max_len=2000),
]

ARC_ENTRY_ROUTE_SCHEMA = ToolSchema(tool_name="arc_board_write", fields=list(_ENTRY_FIELDS))

_ARC_ID_FIELD = FieldSpec("arc_id", str, required=True, max_len=64, pattern=_ARC_ID)

ARC_BRIEF_SCHEMA = ToolSchema(tool_name="arc_brief", fields=[_ARC_ID_FIELD])
ARC_BOARD_READ_SCHEMA = ToolSchema(
    tool_name="arc_board_read",
    fields=[_ARC_ID_FIELD, FieldSpec("after", str, max_len=64)],
)
ARC_BOARD_GREP_SCHEMA = ToolSchema(
    tool_name="arc_board_grep",
    fields=[_ARC_ID_FIELD, FieldSpec("query", str, required=True, max_len=200)],
)
ARC_BOARD_WRITE_SCHEMA = ToolSchema(
    tool_name="arc_board_write", fields=[_ARC_ID_FIELD, *_ENTRY_FIELDS]
)
