"""Companion-declared execution seats, separate from native ACP harnesses."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ExecutionSeatError(ValueError):
    code = "runtime_member_capabilities_unverified"


def validate_member_execution_seat(runtime_agent: str, execution: Any) -> None:
    """Use the existing runtime policy to validate transport, not memory ownership."""
    if execution.member_id is None:
        return
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_MATE_WORKER_KIT,
        ACP_BACKENDS_MEMBER_CAPABILITIES,
    )
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.mate_worker import is_own_worker_execution
    from kiro_crew.platform.context import current_context

    binding = KiroCrewConfig.load().agents.get(runtime_agent)
    lookup = getattr(current_context().providers, "agent_runtime_policy", None)
    policy = (
        lookup(binding.kiro_agent or runtime_agent)
        if binding and not binding.member_id and callable(lookup)
        else None
    )
    worker = is_own_worker_execution(execution)
    native = ACP_BACKENDS_MATE_WORKER_KIT if worker else ACP_BACKENDS_MEMBER_CAPABILITIES
    declared = "member_worker_kit" if worker else "member_capabilities"
    if not isinstance(policy, dict) or not (
        policy.get("backend") in native or policy.get(declared) is True
    ):
        raise ExecutionSeatError(
            "runtime_member_capabilities_unverified: selected seat cannot carry the member kit"
        )


def _validate_seat(row: Any, cfg: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("expected a seat object")
    name, alias = row.get("id"), row.get("runtime_agent")
    if not isinstance(name, str) or not name.strip() or name.strip() != name:
        raise ValueError("requires a nonempty id")
    if not isinstance(alias, str) or not alias:
        raise ValueError("requires a nonempty runtime_agent")
    binding = cfg.agents.get(alias)
    if binding is None or binding.member_id:
        raise ValueError("runtime_agent must be a registered template alias")
    seat: dict[str, Any] = {
        "id": name,
        "runtime_agent": alias,
        "runtime": str(row.get("runtime") or name),
    }
    for key in ("vendor", "role", "account"):
        if key in row:
            value = row[key]
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"requires a nonempty {key}")
            seat[key] = value
    for key, minimum in (("ctx", 1), ("overhead_tokens", 0)):
        if key in row:
            value = row[key]
            if type(value) is not int or value < minimum:
                raise ValueError(f"requires {key} >= {minimum}")
            seat[key] = value
    if "metered" in row:
        if type(row["metered"]) is not bool:
            raise ValueError("requires a boolean metered flag")
        seat["metered"] = row["metered"]
    return seat


def declared_execution_seats(cfg: Any = None) -> dict[str, dict[str, Any]]:
    from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.platform.context import current_context

    getter = getattr(current_context().providers, "execution_seats", None)
    if not callable(getter):
        return {}
    cfg = cfg if cfg is not None else KiroCrewConfig.load()
    rows = getter(cfg)
    if not isinstance(rows, list):
        logger.error("Companion execution_seats must return a list")
        return {}
    result = {}
    for index, row in enumerate(rows):
        label = row.get("id") if isinstance(row, dict) else None
        label = label if isinstance(label, str) and label else f"row {index + 1}"
        try:
            seat = _validate_seat(row, cfg)
            name = seat["id"]
            if name in result:
                raise ValueError("duplicate id")
        except ValueError as exc:
            logger.error("Skipping execution seat %r: %s", label, exc)
            continue
        if name in POLICY_ID_BY_BACKEND.values():
            logger.warning("Skipping execution seat %r: conflicts with a native backend", name)
            continue
        result[name] = seat
    return result
