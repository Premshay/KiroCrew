import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import preference_advisor as handler


@pytest.fixture
def setup(monkeypatch):
    slot = SimpleNamespace(
        key="s",
        memory_mode="persistent",
        is_remote=False,
        total_messages=0,
        _task=None,
        _model_pick_gen=0,
        model="current",
        agent="test-agent",
        project="",
        workspace="",
    )
    app = {
        "state": SimpleNamespace(
            _slots={"s": slot},
            conversation_log=SimpleNamespace(publication_hold=lambda _: nullcontext()),
        )
    }
    body = {"slot": "s", "task": "classify", "models": ["small"]}
    monkeypatch.setattr(handler, "_deny_non_owner", AsyncMock(return_value=None))
    from kiro_crew.dashboard import chat_utils

    monkeypatch.setattr(chat_utils, "effective_session_key", lambda _: "dashboard:s")

    async def read(_):
        return body, None

    monkeypatch.setattr(handler, "read_bounded_json", read)
    monkeypatch.setattr(
        handler,
        "_compute",
        lambda *args: {
            "model": "small",
            "budget": "fast",
            "reason": "similar_preferences",
            "examples": ["a", "b"],
        },
    )
    return SimpleNamespace(app=app, get=lambda key: None), slot, body


class TestPreferenceAdvisor:
    def test_consult_tool_uses_verified_identity_and_bounded_transport(self, monkeypatch):
        from unittest.mock import Mock

        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools.ledger import preference_advice

        post = Mock(return_value={"reason": "no_examples", "advisory": True})
        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda _: ("", "unverified"))
        assert preference_advice("preference_advice", {}) == "unverified"
        post.assert_not_called()
        monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda _: ("dashboard:s", ""))
        args = {"task": "triage findings", "role": "worker", "models": ["small"]}
        assert json.loads(preference_advice("preference_advice", args))["advisory"] is True
        post.assert_called_once_with(
            "/api/preference-consult", args, session_key="dashboard:s", timeout=5
        )

    @pytest.mark.asyncio
    async def test_slotless_preview_does_not_create_slot_or_feedback_token(self, setup):
        request, _, body = setup
        body["slot"] = ""
        before = dict(request.app["state"]._slots)
        result = json.loads((await handler.api_preference_advice(request)).body)
        assert result["preview"] is True
        assert result["model"] == "small"
        assert "id" not in result
        assert request.app["state"]._slots == before
        assert handler._KEY not in request.app

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["parent", "worker"])
    async def test_consult_during_ongoing_task_is_read_only(self, setup, monkeypatch, role):
        from kiro_crew.dashboard.handlers import _shared, cron

        request, slot, body = setup
        slot.total_messages = 50
        request.headers = {"X-Session-Key": "dashboard:s"}
        body["role"] = role
        recognize = AsyncMock(return_value=None)
        monkeypatch.setattr(cron, "_recognize_session", recognize)
        monkeypatch.setattr(_shared, "_is_restricted_session", lambda *_: False)
        score = AsyncMock(return_value={"model": "small", "evidence": ["private task"]})
        monkeypatch.setattr(handler, "_score", score)
        result = json.loads((await handler.api_preference_consult(request)).body)
        assert result == {"model": "small", "advisory": True, "role": role}
        assert score.call_args.kwargs["role"] == role
        assert score.call_args.kwargs["session_key"] == "dashboard:s"
        assert slot.model == "current"
        assert slot.total_messages == 50
        assert handler._KEY not in request.app

    @pytest.mark.asyncio
    async def test_restricted_consult_never_scores(self, setup, monkeypatch):
        from kiro_crew.dashboard.handlers import _shared, cron

        request, _, body = setup
        request.headers = {"X-Session-Key": "dashboard:s"}
        body["role"] = "worker"
        monkeypatch.setattr(cron, "_recognize_session", AsyncMock(return_value=None))
        monkeypatch.setattr(_shared, "_is_restricted_session", lambda *_: True)
        score = AsyncMock()
        monkeypatch.setattr(handler, "_score", score)
        assert (await handler.api_preference_consult(request)).status == 403
        score.assert_not_called()

    @pytest.mark.asyncio
    async def test_consult_refuses_app_before_scoring(self, setup, monkeypatch):
        request, _, _ = setup
        request.get = lambda key: "app-principal" if key == "app" else None
        score = AsyncMock()
        monkeypatch.setattr(handler, "_score", score)
        assert (await handler.api_preference_consult(request)).status == 403
        score.assert_not_called()

    @pytest.mark.asyncio
    async def test_consult_refuses_unrecognized_session(self, setup, monkeypatch):
        from kiro_crew.dashboard.handlers import cron

        request, _, _ = setup
        request.headers = {"X-Session-Key": "dashboard:unknown"}
        monkeypatch.setattr(
            cron, "_recognize_session", AsyncMock(return_value=web.Response(status=400))
        )
        score = AsyncMock()
        monkeypatch.setattr(handler, "_score", score)
        assert (await handler.api_preference_consult(request)).status == 400
        score.assert_not_called()

    @pytest.mark.asyncio
    async def test_consult_withholds_result_after_privacy_change(self, setup, monkeypatch):
        from kiro_crew.dashboard.handlers import _shared, cron

        request, _, body = setup
        request.headers = {"X-Session-Key": "dashboard:s"}
        body["role"] = "parent"
        monkeypatch.setattr(cron, "_recognize_session", AsyncMock(return_value=None))
        restricted = iter([False, True])
        monkeypatch.setattr(_shared, "_is_restricted_session", lambda *_: next(restricted))
        score = AsyncMock(return_value={"model": "small", "budget": "fast"})
        monkeypatch.setattr(handler, "_score", score)
        result = await handler.api_preference_consult(request)
        assert result.status == 403
        assert "budget" not in json.loads(result.body)
        score.assert_awaited_once()

    @pytest.mark.parametrize(
        "contents,enabled",
        [
            (None, False),
            ('{"enabled":false}', False),
            ('{"enabled":true}', True),
            ("{", False),
        ],
    )
    def test_enablement_requires_valid_explicit_configuration(
        self, tmp_path, monkeypatch, contents, enabled
    ):
        from kiro_crew.config import paths

        monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
        if contents is not None:
            (tmp_path / "routing-preferences.json").write_text(contents)
        assert handler.preference_advisor_enabled() is enabled

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "field,value",
        [
            ("memory_mode", "incognito"),
            ("memory_mode", "temporary"),
            ("_pending_memory_mode", "incognito"),
            ("total_messages", 1),
            ("_model_pick_gen", 1),
            ("is_remote", True),
        ],
    )
    async def test_restricted_or_continuing_sessions_never_score(
        self, setup, monkeypatch, field, value
    ):
        request, slot, _ = setup
        setattr(slot, field, value)

        def forbidden(*args):
            pytest.fail("must not read examples or encode")

        monkeypatch.setattr(handler, "_compute", forbidden)
        response = await handler.api_preference_advice(request)
        assert json.loads(response.body)["reason"] == "keep_current"

    @pytest.mark.asyncio
    async def test_non_owner_cannot_read_or_write(self, setup, monkeypatch):
        request, _, _ = setup
        monkeypatch.setattr(
            handler, "_deny_non_owner", AsyncMock(return_value=web.Response(status=403))
        )
        assert (await handler.api_preference_advice(request)).status == 403
        assert (await handler.api_preference_feedback(request)).status == 403

    @pytest.mark.asyncio
    async def test_feedback_requires_successful_model_pick_and_is_idempotent(
        self, setup, monkeypatch
    ):
        from kiro_crew.dashboard import chat_utils
        from kiro_crew.decisions import log

        request, slot, body = setup
        rows = []
        monkeypatch.setattr(log, "append", lambda row: rows.append(row) or True)
        monkeypatch.setattr(chat_utils, "effective_session_key", lambda _: "dashboard:s")
        advice = json.loads((await handler.api_preference_advice(request)).body)
        assert slot.model == "current"
        assert rows == []
        body.clear()
        body.update(id=advice["id"], choice="use", model="small")
        assert (await handler.api_preference_feedback(request)).status == 409
        slot.model = "small"
        assert (await handler.api_preference_feedback(request)).status == 200
        assert (await handler.api_preference_feedback(request)).status == 200
        assert len(rows) == 1
        assert rows[0]["kind"] == "preference_feedback"
        assert "classify" not in json.dumps(rows)

    @pytest.mark.asyncio
    async def test_feedback_after_privacy_change_is_not_written(self, setup):
        request, slot, body = setup
        advice = json.loads((await handler.api_preference_advice(request)).body)
        slot.memory_mode = "incognito"
        body.clear()
        body.update(id=advice["id"], choice="keep", model="current")
        assert (await handler.api_preference_feedback(request)).status == 409

    @pytest.mark.asyncio
    async def test_persisted_privacy_restriction_blocks_feedback(self, setup, monkeypatch):
        from kiro_crew.decisions import log
        from kiro_crew.history import TranscriptWithheld

        request, _, body = setup
        advice = json.loads((await handler.api_preference_advice(request)).body)

        def refused(_):
            raise TranscriptWithheld("restricted")

        request.app["state"].conversation_log.publication_hold = refused

        def forbidden(_):
            pytest.fail("restricted feedback must not reach the log")

        monkeypatch.setattr(log, "append", forbidden)
        body.clear()
        body.update(id=advice["id"], choice="keep", model="current")
        assert (await handler.api_preference_feedback(request)).status == 409

    def test_missing_config_is_off(self, tmp_path, monkeypatch):
        from kiro_crew.config import paths

        monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
        assert handler._compute("task", ["small"]) == {"reason": "not_configured"}

    def test_only_reviewed_examples_reach_local_encoder(self, tmp_path, monkeypatch):
        from kiro_crew import decisions, embeddings
        from kiro_crew.config import paths

        monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(decisions, "is_enabled", lambda *a, **kw: False)
        calls = []

        class LocalEncoder:
            model_id = "local-model"

            def embed(self, text):
                calls.append(text)
                return [1, 0]

        monkeypatch.setattr(handler, "_example_vectors", {})
        monkeypatch.setattr(embeddings, "LlamaCppEmbedder", LocalEncoder)
        monkeypatch.setattr(embeddings, "get_shared_embedder", LocalEncoder)
        rows = [
            {
                "id": str(i),
                "group": str(i),
                "task": f"reviewed {i}",
                "role": "parent",
                "budget": "balanced",
                "reviewed": True,
            }
            for i in range(2)
        ]
        rows.append({"task": "unreviewed private example", "reviewed": False})
        (tmp_path / "routing-preferences.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "models": {"balanced": "small"},
                    "examples": rows,
                }
            ),
            encoding="utf-8",
        )
        result = handler._compute("new task", ["small"])
        assert result["model"] == "small"
        assert calls == ["new task", "reviewed 0", "reviewed 1"]
        assert result["evidence"] == ["reviewed 0", "reviewed 1"]
        assert handler._compute("worker task", ["small"], role="worker")["reason"] == "no_examples"
        assert len(calls) == 3
        monkeypatch.setattr(decisions, "is_enabled", lambda *a, **kw: True)
        assert (
            handler._compute("new task", ["small"], routing_armed=True)["reason"]
            == "automatic_routing_active"
        )
        assert len(calls) == 3
        # Reviewed examples are embedded once per model; later requests embed
        # only their own task.
        assert handler._compute("new task", ["small"], routing_armed=False)["model"] == "small"
        assert len(calls) == 4
        unmapped = handler._compute("new task", ["another-provider-model"], require_mapping=False)
        assert unmapped["budget"] == "balanced"
        assert unmapped["model"] is None
        assert len(calls) == 5
        # An exhausted budget is an answer, not an exception escaping the worker.
        assert handler._compute("new task", ["small"], deadline=0) == {"reason": "advice_timeout"}
        assert len(calls) == 5
        LocalEncoder.model_id = "another-model"
        handler._compute("new task", ["small"])
        assert calls[-3:] == ["new task", "reviewed 0", "reviewed 1"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model,flag,expected",
        [("auto", False, True), ("current", True, True), ("current", False, False)],
    )
    async def test_passes_actual_slot_routing_state(
        self, setup, monkeypatch, model, flag, expected
    ):
        request, slot, _ = setup
        slot.model, slot.jev_route = model, flag
        seen = []
        monkeypatch.setattr(handler, "_compute", lambda *args: seen.append(args[3]) or {})
        await handler.api_preference_advice(request)
        assert seen == [expected]

    @pytest.mark.asyncio
    async def test_slow_scoring_is_bounded_without_holding_feedback_lock(self, setup, monkeypatch):
        request, slot, body = setup
        advice = json.loads((await handler.api_preference_advice(request)).body)
        entered, release = asyncio.Event(), asyncio.Event()
        original = asyncio.to_thread

        async def blocked(function, *args):
            if function is handler._compute:
                entered.set()
                await release.wait()
                return {"model": "small"}
            return await original(function, *args)

        monkeypatch.setattr(handler.asyncio, "to_thread", blocked)
        monkeypatch.setattr(handler, "_COMPUTE_SECONDS", 0.02)
        pending = asyncio.create_task(handler.api_preference_advice(request))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert (
                json.loads((await handler.api_preference_advice(request)).body)["reason"]
                == "advice_busy"
            )
            from kiro_crew.decisions import log

            monkeypatch.setattr(log, "append", lambda row: True)
            body.clear()
            body.update(id=advice["id"], choice="keep", model="current")
            assert (
                await asyncio.wait_for(handler.api_preference_feedback(request), 1)
            ).status == 200
            assert (
                json.loads((await asyncio.wait_for(pending, 1)).body)["reason"] == "advice_timeout"
            )
            assert not request.app[handler._WORK].done()
        finally:
            release.set()
            await asyncio.wait_for(request.app[handler._WORK], 1)
            await pending


def test_consult_route_is_strict_internal() -> None:
    """The preference_advice tool authenticates with the internal secret.

    Without the strict entry its call falls through to cookie auth and every
    consultation answers "Token required"; an upstream sync dropped it once.
    """
    from kiro_crew.dashboard.server import (
        _MIXED_INTERNAL_API_PATHS,
        _STRICT_INTERNAL_API_PATHS,
    )

    path = "/api/preference-consult"
    assert path in _STRICT_INTERNAL_API_PATHS
    assert path not in _MIXED_INTERNAL_API_PATHS
