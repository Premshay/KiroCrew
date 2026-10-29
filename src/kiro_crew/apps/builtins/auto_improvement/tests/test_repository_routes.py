from __future__ import annotations

import json

import pytest

from ..backend import repository_routes, routes, store
from .request_helpers import owner_request


@pytest.fixture
def survey_route(tmp_path, monkeypatch):
    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    monkeypatch.setattr(repository_routes.clone_setup, "_repository_is_isolated", lambda _: True)
    config = {"clone": str(tmp_path), "branch": "origin/feature"}
    monkeypatch.setattr(repository_routes, "resolve_survey_ref", lambda root, branch: branch)
    store.write_json_atomic(store.config_path(), config)
    return config


@pytest.mark.asyncio
async def test_survey_reads_saved_branch_without_changing_config(survey_route, monkeypatch):
    def survey(root, ref):
        assert str(root) == survey_route["clone"]
        assert ref == "origin/feature"
        return {"revision": "abc", "workloads": []}

    monkeypatch.setattr(repository_routes, "survey_repository", survey)
    response = await repository_routes.handle_repository_survey(
        owner_request("GET", "/repository/survey")
    )
    assert response.status == 200
    assert json.loads(response.text)["revision"] == "abc"
    assert store.read_json(store.config_path()) == survey_route


@pytest.mark.asyncio
async def test_invalid_declaration_is_visible_and_preserves_config(survey_route, monkeypatch):
    def survey(*_):
        raise ValueError("Invalid repository declaration JSON")

    monkeypatch.setattr(repository_routes, "survey_repository", survey)
    response = await repository_routes.handle_repository_survey(
        owner_request("GET", "/repository/survey")
    )
    assert response.status == 400
    assert "declaration" in json.loads(response.text)["error"]
    assert store.read_json(store.config_path()) == survey_route


@pytest.mark.asyncio
async def test_survey_refuses_run_started_while_waiting(survey_route, monkeypatch):
    monkeypatch.setattr(routes, "_run_is_active", lambda: True)
    response = await repository_routes.handle_repository_survey(
        owner_request("GET", "/repository/survey")
    )
    assert response.status == 409


@pytest.mark.asyncio
async def test_survey_checks_repository_isolation(survey_route, monkeypatch):
    monkeypatch.setattr(repository_routes.clone_setup, "_repository_is_isolated", lambda _: False)
    response = await repository_routes.handle_repository_survey(
        owner_request("GET", "/repository/survey")
    )
    assert response.status == 400
    assert "isolation" in json.loads(response.text)["error"]


@pytest.mark.asyncio
async def test_survey_normalizes_a_bare_branch(survey_route, monkeypatch):
    store.write_json_atomic(store.config_path(), {**survey_route, "branch": "feature"})
    seen = []

    def resolve(root, branch):
        assert branch == "feature"
        return "refs/heads/feature"

    monkeypatch.setattr(repository_routes, "resolve_survey_ref", resolve)

    def survey(root, ref):
        seen.append(ref)
        return {"revision": "abc", "workloads": []}

    monkeypatch.setattr(repository_routes, "survey_repository", survey)
    response = await repository_routes.handle_repository_survey(
        owner_request("GET", "/repository/survey")
    )
    assert response.status == 200
    assert seen == ["refs/heads/feature"]
