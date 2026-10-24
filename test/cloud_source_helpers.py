"""Account-local cloud staging fixtures independent of host ancestor ownership."""

import pytest


@pytest.fixture
def source_account_home(tmp_path, monkeypatch):
    """Keep the default data home inside the test account's own directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "default-data-home"))
