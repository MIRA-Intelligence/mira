"""OAuth token storage path helpers."""

from __future__ import annotations

import os
from pathlib import Path

_XDG_ENV_NAMES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")


def ensure_oauth_state_dirs_for_runtime() -> None:
    """Create OAuth state dirs without changing native desktop defaults."""
    if any(os.environ.get(name) for name in _XDG_ENV_NAMES):
        for name in _XDG_ENV_NAMES:
            value = os.environ.get(name)
            if not value:
                continue
            expanded = str(Path(value).expanduser())
            os.environ[name] = expanded
            Path(expanded).mkdir(parents=True, exist_ok=True)
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            (Path(config_home).expanduser() / "litellm").mkdir(parents=True, exist_ok=True)
        return

    if Path.home() != Path("/home/mira"):
        return

    # Container images run as /home/mira and mount ~/.mira for persistence.
    writable_home = Path.home() / ".mira"
    writable_home.mkdir(parents=True, exist_ok=True)
    (writable_home / ".config" / "litellm").mkdir(parents=True, exist_ok=True)
    (writable_home / ".local" / "share").mkdir(parents=True, exist_ok=True)
    (writable_home / ".cache").mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CONFIG_HOME"] = str(writable_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(writable_home / ".local" / "share")
    os.environ["XDG_CACHE_HOME"] = str(writable_home / ".cache")
