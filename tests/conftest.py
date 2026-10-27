from unittest.mock import patch

import pytest


def _register_bundled_plugin_packages() -> None:
    """First-party plugins (repo ``plugins/``) import as ``mira_plugins.<id>``,
    the way the plugin runtime imports them; register them up front so tests
    can import and patch their modules (``mira_plugins.telegram.telegram_channel``)."""
    from mira_engine.plugins.loader import _package_for
    from mira_engine.plugins.manifest import load_manifest
    from mira_engine.plugins.paths import bundled_plugins_dir

    root = bundled_plugins_dir()
    for plugin_dir in sorted(root.iterdir()) if root else []:
        if (plugin_dir / "plugin.json").is_file():
            _package_for(load_manifest(plugin_dir))


_register_bundled_plugin_packages()


@pytest.fixture(autouse=True)
def isolate_runtime_home(tmp_path, monkeypatch):
    """Tests must not read or modify a developer's live gateway data."""
    from mira_engine.config import loader

    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "mira-home"))
    monkeypatch.setenv("MIRA_RUNTIME_DIR", str(tmp_path / "mira-run"))
    monkeypatch.delenv("MIRA_CONFIG_PATH", raising=False)
    monkeypatch.setattr(loader, "_current_config_path", None)
    # The host policy (/etc/mira/policy.toml) of the machine running the tests
    # must not change their outcome.
    from mira_engine.security import host_policy

    monkeypatch.setattr(host_policy, "POLICY_PATH", tmp_path / "etc-mira" / "policy.toml")
    host_policy.reset_cache()
    # The plugin runtime is process-wide; load it fresh for each test's home.
    from mira_engine.plugins import set_runtime_for_tests

    set_runtime_for_tests(None)
    yield
    set_runtime_for_tests(None)
    # Gateway tests that stop before the event loop leave their lease to atexit.
    from mira_engine.cli.engine_lease import release_all_leases

    release_all_leases()

@pytest.fixture(autouse=True)
def mock_gateway_failsafe():
    """Globally mock the gateway failsafe check to avoid PID/port collision issues in tests."""
    with patch("mira_engine.cli.commands._gateway_failsafe_check") as mock, \
            patch("mira_engine.cli.commands._free_gateway_port_or_exit"):
        yield mock
