"""OAuth token storage path helpers."""

from __future__ import annotations

import os
from pathlib import Path

_XDG_ENV_NAMES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")


def _expand_xdg_path(value: str) -> Path:
    """Expand XDG paths consistently when tests or launchers override HOME."""
    home = os.environ.get("HOME")
    if home and (value == "~" or value.startswith(("~/", "~\\"))):
        remainder = value[2:] if len(value) > 1 else ""
        return Path(home) / remainder
    return Path(value).expanduser()


def ensure_oauth_state_dirs_for_runtime() -> None:
    """Create OAuth state dirs without changing native desktop defaults."""
    if any(os.environ.get(name) for name in _XDG_ENV_NAMES):
        for name in _XDG_ENV_NAMES:
            value = os.environ.get(name)
            if not value:
                continue
            expanded = str(_expand_xdg_path(value))
            os.environ[name] = expanded
            Path(expanded).mkdir(parents=True, exist_ok=True)
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            (_expand_xdg_path(config_home) / "litellm").mkdir(parents=True, exist_ok=True)
        return

    if Path.home() != Path("/home/mira"):
        return

    # Container images run as /home/mira and mount ~/.mira for persistence.
    from mira_engine.config.loader import get_home_dir

    writable_home = get_home_dir()
    writable_home.mkdir(parents=True, exist_ok=True)
    (writable_home / ".config" / "litellm").mkdir(parents=True, exist_ok=True)
    (writable_home / ".local" / "share").mkdir(parents=True, exist_ok=True)
    (writable_home / ".cache").mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CONFIG_HOME"] = str(writable_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(writable_home / ".local" / "share")
    os.environ["XDG_CACHE_HOME"] = str(writable_home / ".cache")
