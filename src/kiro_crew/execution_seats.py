"""Companion-declared execution seats, separate from native ACP harnesses."""

from __future__ import annotations

from typing import Any


def declared_execution_seats(cfg: Any = None) -> dict[str, dict[str, str]]:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND
    from kiro_crew.platform.context import current_context

    getter = getattr(current_context().providers, "execution_seats", None)
    if not callable(getter):
        return {}
    cfg = cfg if cfg is not None else KiroCrewConfig.load()
    rows = getter(cfg)
    if not isinstance(rows, list):
        raise ValueError("Companion execution_seats must return a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Companion execution seat must be an object")
        name, alias = row.get("id"), row.get("runtime_agent")
        if not isinstance(name, str) or not name or not isinstance(alias, str) or not alias:
            raise ValueError("Companion execution seat requires id and runtime_agent")
        binding = cfg.agents.get(alias)
        if binding is None or binding.member_id:
            raise ValueError(f"Execution seat {name!r} requires a registered template alias")
        if name in result:
            raise ValueError(f"Duplicate execution seat {name!r}")
        if name in POLICY_ID_BY_BACKEND.values():
            raise ValueError(f"Execution seat {name!r} conflicts with a native backend")
        result[name] = {
            "id": name,
            "runtime_agent": alias,
            "runtime": str(row.get("runtime") or name),
        }
    return result
