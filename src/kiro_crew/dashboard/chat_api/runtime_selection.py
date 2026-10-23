"""Execution-seat selection, independent of the conversation member."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import DashboardState


if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _TEARDOWN_INCOMPLETE_WARNING,
        KiroCrewConfig,
        LLMProvider,
        _reset_slot_session_or_warn,
        _slot_not_found,
        _slot_switch_session_lock,
        _subagents_attached_response,
        _switch_target_busy,
        canonical_key,
        deny_app_slot_access,
        effective_session_key,
        members_mod,
        read_bounded_json,
    )


async def api_chat_slot_runtime(request: web.Request) -> web.Response:
    """Select an execution seat without changing the conversation's member."""
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MEMBER_CAPABILITIES
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "chat.slot_runtime")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    candidate_slot = state._slots.get(name)
    if candidate_slot is None:
        return _slot_not_found()
    slot = candidate_slot
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    from kiro_crew.platform.context import current_context

    getter = getattr(current_context().providers, "agent_runtime_policy", None)
    choices = []
    seen = set()
    member = cfg.agents.get(slot.agent)
    member_bound = slot.mode == members_mod.DM_SLOT_MODE or bool(member and member.member_id)
    if callable(getter):
        for alias, binding in cfg.agents.items():
            if binding.member_id:
                continue
            # Internal roles are not operator execution seats: kirocrew-* are the
            # gateway's own background agents, code-review-sage-reviewer is the
            # review pool's identity, and mochi* are app seats. Listing them let
            # a task-specific engine (the knowledge-extraction Codex seat) be
            # chosen and labelled as the vendor, so the picker offered a seat
            # whose model was never the operator's intent.
            if alias.startswith(("kirocrew-", "mochi")) or alias == "code-review-sage-reviewer":
                continue
            policy = getter(binding.kiro_agent or alias)
            if not isinstance(policy, dict) or not policy.get("engine"):
                continue
            engine = policy["engine"]
            if engine in seen:
                continue
            seen.add(engine)
            # A seat outside the ACP backends (a companion provider) declares its
            # own support; it is held to the same loaded_stamp at allocation.
            member_capable = (
                policy.get("backend") in ACP_BACKENDS_MEMBER_CAPABILITIES
                or policy.get("member_capabilities") is True
            )
            supported = not member_bound or member_capable
            choices.append(
                {
                    "name": alias,
                    "label": policy.get("runtime") or engine,
                    "engine": engine,
                    "supported": supported,
                    "priority": policy.get("priority", 5),
                    "backend": policy.get("backend"),
                    "member_capable": member_capable,
                    "reason": "" if supported else "member capabilities not verified here",
                }
            )
    choices.sort(key=lambda row: row["priority"])
    # The seat the conversation already runs on, so an unset runtime_agent reads
    # as the backend in force rather than a bare "Default". Resolved through the
    # same policy getter as the choices above, so a private engine copy inherits
    # its parent's engine exactly as allocation does.
    effective_runtime_agent = ""
    if callable(getter) and slot.agent:
        slot_binding = cfg.agents.get(slot.agent)
        for lookup in (slot.agent, getattr(slot_binding, "kiro_agent", "") or ""):
            if not lookup:
                continue
            policy = getter(lookup)
            if isinstance(policy, dict) and policy.get("engine"):
                for row in choices:
                    if row["engine"] == policy["engine"]:
                        effective_runtime_agent = row["name"]
                        break
                break
    if request.method == "GET":
        return web.json_response(
            {
                "runtime_agent": slot.runtime_agent,
                "effective_runtime_agent": effective_runtime_agent,
                "choices": choices,
            }
        )
    body, error = await read_bounded_json(request)
    if error is not None:
        return error
    assert body is not None
    selected = body.get("runtime_agent")
    if not isinstance(selected, str):
        return web.json_response(
            {"error": "invalid execution seat", "code": "invalid_execution_seat"}, status=400
        )
    if selected and not any(row["name"] == selected and row["supported"] for row in choices):
        return web.json_response(
            {
                "error": "This execution seat cannot verify saved member capabilities",
                "code": "execution_seat_capabilities_unverified",
            },
            status=409,
        )
    async with slot._lock:
        session_key = effective_session_key(slot)
        async with _slot_switch_session_lock(session_key), slot._model_pick_lock:
            if state._slots.get(name) is not slot:
                return _slot_not_found()
            member = cfg.agents.get(slot.agent)
            if selected and (
                slot.mode == members_mod.DM_SLOT_MODE or (member and member.member_id)
            ):
                selected_row = next(row for row in choices if row["name"] == selected)
                if not selected_row["member_capable"]:
                    return web.json_response(
                        {
                            "error": "Saved member capabilities are not verified on this runtime",
                            "code": "runtime_member_capabilities_unverified",
                        },
                        status=409,
                    )
            denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_runtime")
            if denied is not None:
                return denied
            denied = await _subagents_attached_response(state, slot, session_key, "slot_runtime")
            if denied is not None:
                return denied
            provider = state.sessions.get_provider(session_key)
            if _switch_target_busy(state, slot, session_key, provider):
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            siblings = [
                other
                for other in state._slots.values()
                if canonical_key(effective_session_key(other)) == canonical_key(session_key)
            ]
            if any(
                other.agent != slot.agent or other.memory_store != slot.memory_store
                for other in siblings
            ):
                return web.json_response(
                    {
                        "error": "Linked views disagree on conversation identity; reload the conversation before switching",
                        "code": "linked_views_disagree",
                    },
                    status=409,
                )
            if all(other.runtime_agent == selected for other in siblings):
                return web.json_response(
                    {
                        "ok": True,
                        "runtime_agent": selected,
                        "model": slot.model,
                        "reasoning_effort": slot.reasoning_effort,
                        "served_model": slot.served_model,
                    }
                )
            updates = {
                "runtime_agent": selected,
                "model": "",
                "reasoning_effort": "",
                "served_model": "",
                "_active_fallback_model": "",
                "_fallback_primary_model": "",
                "_fallback_slot_model": "",
                "_fallback_candidate_idx": 0,
                "_fallback_walked": [],
                "jev_route": False,
                "_dirty": True,
            }
            previous = [
                (other, {field: getattr(other, field) for field in updates}) for other in siblings
            ]

            def rollback():
                for other, values in previous:
                    if other.runtime_agent == selected:
                        for field, value in values.items():
                            setattr(other, field, value)
                        other._dirty = True
                state.push_slots_update()

            for other in siblings:
                for field, value in updates.items():
                    setattr(other, field, value.copy() if isinstance(value, list) else value)

            async def attempt_teardown() -> bool | None:
                """One teardown attempt, sharing the raise handling with the retry."""
                try:
                    return await _reset_slot_session_or_warn(
                        state, slot, session_key, switch_kind="runtime"
                    )
                except asyncio.CancelledError:
                    if (
                        provider is not None
                        and state.sessions.get_provider(session_key) is provider
                    ):
                        rollback()
                    else:
                        state.push_slots_update()
                    raise
                except Exception:
                    rollback()
                    raise

            teardown_incomplete = False
            reset = await attempt_teardown()
            if reset is None:
                # Teardown raised after the session pop: the switch is committed, so
                # answer the committed state with an advisory warning.
                teardown_incomplete = True
            elif not reset:
                # A False verdict is ambiguous: a turn slipped into the window
                # between the fast-path probe and the atomic pop, or there was no
                # live session to tear down at all. Disambiguate fail-closed on the
                # live provider, exactly as the model/effort switch handlers do --
                # with no registered provider the next message cold-starts on the
                # selected seat, which is what the reset would have arranged.
                busy_provider = state.sessions.get_provider(session_key)
                if isinstance(busy_provider, LLMProvider):
                    if busy_provider.has_active_turn():
                        rollback()
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
                    # A live IDLE session declined (its turn ended before this
                    # re-read). Tearing down an idle session is safe -- history
                    # lives on the slot, not the process -- so retry once; a
                    # second decline means a turn is genuinely racing.
                    reset = await attempt_teardown()
                    if reset is None:
                        teardown_incomplete = True
                    elif not reset:
                        rollback()
                        return web.json_response(
                            {"error": "a turn is in flight", "code": "turn_in_flight"},
                            status=409,
                        )
            if effective_session_key(slot) != session_key:
                rollback()
                return web.json_response(
                    {
                        "error": "session changed during execution switch",
                        "code": "session_changed_during_execution_switch",
                    },
                    status=409,
                )
            # The teardown alone resumes the stored conversation when the new seat
            # shares the old one's backend label; mark it so allocation starts fresh.
            state.sessions._session_map.mark_seat_switch(session_key)
            state.push_slots_update()
            response = {
                "ok": True,
                "runtime_agent": selected,
                "model": "",
                "reasoning_effort": "",
                "served_model": "",
            }
            if teardown_incomplete:
                response["warning"] = _TEARDOWN_INCOMPLETE_WARNING
            return web.json_response(response)
