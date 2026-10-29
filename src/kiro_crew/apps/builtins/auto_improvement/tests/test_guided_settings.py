from __future__ import annotations

import json

import pytest

from ..backend import routes, store
from ..backend.commit import manual_regression_required
from ..backend.run_settings import normalize_run_settings
from .request_helpers import owner_request


@pytest.mark.parametrize(
    "patch",
    [
        {"maxCycles": True},
        {"maxCycles": 1.5},
        {"maxHours": 0},
        {"maxCostUsd": float("nan")},
        {"calibrationReps": 11},
        {"measureReps": 1},
        {"maxHours": 10**400},
        {"editAllowlist": ["../other/**"]},
        {"editAllowlist": ["/etc/**"]},
        {"editAllowlist": [".git/**"]},
        {"editAllowlist": "src/**"},
        {"scopeDiffBase": "--output=/tmp/x"},
        {"scopeDiffBase": "HEAD\n"},
    ],
)
def test_invalid_settings_rejected(patch):
    with pytest.raises(ValueError):
        normalize_run_settings(patch)


def test_scope_and_budget_round_trip_between_repositories():
    config = {
        "target_url": "https://github.com/acme/a",
        "editAllowlist": ["src/**"],
        "maxCycles": 3,
        "maxHours": 1,
        "suiteTimeoutSeconds": 2400,
    }
    store.remember_test_environment(config)
    config["target_url"] = "https://github.com/acme/b"
    store.restore_test_environment(config)
    assert config["editAllowlist"] == []
    assert config["maxCycles"] == 25
    assert config["suiteTimeoutSeconds"] == 900
    config["target_url"] = "https://github.com/acme/a"
    store.restore_test_environment(config)
    assert config["editAllowlist"] == ["src/**"]
    assert config["maxCycles"] == 3
    assert config["suiteTimeoutSeconds"] == 2400


def test_manual_publication_requires_regression_for_implicit_scope(tmp_path):
    (tmp_path / "orders" / "tests").mkdir(parents=True)
    assert manual_regression_required(
        {"clone": str(tmp_path), "editAllowlist": ["orders/**"]}, "candidate"
    )


@pytest.mark.asyncio
async def test_invalid_budget_preserves_saved_config(monkeypatch):
    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    original = {"branch": "main", "maxCycles": 7}
    store.write_json_atomic(store.config_path(), original)
    request = owner_request("PUT", "/config")
    request._read_bytes = json.dumps({"branch": "other", "maxCycles": False}).encode()
    response = await routes._handle_put_config(request)
    assert response.status == 400
    assert store.read_json(store.config_path()) == original


@pytest.mark.asyncio
async def test_timeout_is_saved_and_invalidates_calibration(monkeypatch):
    async def free(_):
        return None

    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    original = {"target_url": "https://github.com/acme/a", "branch": "main"}
    store.write_json_atomic(store.config_path(), original)
    ruler = store.data_dir() / "repos" / store.workspace_key(original) / "ruler" / "ruler.json"
    store.write_json_atomic(ruler, {"status": "calibrated"})
    request = owner_request("PUT", "/config")
    request._read_bytes = json.dumps({"suiteTimeoutSeconds": 2400}).encode()
    response = await routes._handle_put_config(request)
    assert response.status == 200
    assert json.loads(response.text)["rejected"] == []
    assert store.read_json(store.config_path())["suiteTimeoutSeconds"] == 2400
    assert store.read_json(ruler) == {"status": "uncalibrated"}
