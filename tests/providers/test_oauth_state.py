from __future__ import annotations

from pathlib import Path

import mira_engine.providers.oauth_state as oauth_state
from mira_engine.providers.oauth_state import ensure_oauth_state_dirs_for_runtime


def _clear_xdg_env(monkeypatch) -> None:
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_ensure_oauth_state_dirs_does_not_override_native_home(monkeypatch, tmp_path) -> None:
    _clear_xdg_env(monkeypatch)
    monkeypatch.setattr(oauth_state.Path, "home", staticmethod(lambda: tmp_path))

    ensure_oauth_state_dirs_for_runtime()

    assert "XDG_CONFIG_HOME" not in oauth_state.os.environ
    assert "XDG_DATA_HOME" not in oauth_state.os.environ
    assert "XDG_CACHE_HOME" not in oauth_state.os.environ
    assert not (tmp_path / ".mira").exists()


def test_ensure_oauth_state_dirs_expands_existing_xdg_dirs(monkeypatch, tmp_path) -> None:
    _clear_xdg_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "~/xdg-config")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    ensure_oauth_state_dirs_for_runtime()

    assert oauth_state.os.environ["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")
    assert oauth_state.os.environ["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")
    assert "XDG_CACHE_HOME" not in oauth_state.os.environ
    assert (tmp_path / "xdg-config" / "litellm").is_dir()
    assert (tmp_path / "xdg-data").is_dir()
