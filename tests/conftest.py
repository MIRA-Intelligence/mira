from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_home(tmp_path, monkeypatch):
    """Tests must not read or modify a developer's live gateway data."""
    from mira_engine.config import loader

    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "mira-home"))
    monkeypatch.setenv("MIRA_RUNTIME_DIR", str(tmp_path / "mira-run"))
    monkeypatch.delenv("MIRA_CONFIG_PATH", raising=False)
    monkeypatch.setattr(loader, "_current_config_path", None)
    yield
    # Gateway tests that stop before the event loop leave their lease to atexit.
    from mira_engine.cli.engine_lease import release_all_leases

    release_all_leases()

@pytest.fixture(autouse=True)
def mock_gateway_failsafe():
    """Globally mock the gateway failsafe check to avoid PID/port collision issues in tests."""
    with patch("mira_engine.cli.commands._gateway_failsafe_check") as mock:
        yield mock
