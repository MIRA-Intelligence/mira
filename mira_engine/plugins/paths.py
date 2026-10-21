"""Where plugins live. Imports nothing from the agent or the config schema, so
code that runs while the config is being loaded (provider lookups, the skill
pack manager) can find plugins without an import cycle."""

from __future__ import annotations

import sys
from pathlib import Path


def bundled_plugins_dir() -> Path | None:
    """First-party plugins: shipped as ``mira_engine/_bundled_plugins`` in the
    wheel and the desktop build, or the repo's ``plugins/`` in a source checkout."""
    import mira_engine

    pkg = Path(mira_engine.__file__).resolve().parent
    shipped = pkg / "_bundled_plugins"
    if shipped.is_dir():
        return shipped
    repo = pkg.parent
    if (repo / "pyproject.toml").is_file() and (repo / "plugins").is_dir():
        return repo / "plugins"
    return None


def user_plugins_dir() -> Path:
    from mira_engine.config.paths import get_data_dir

    return get_data_dir() / "plugins"


def host_command() -> list[str]:
    """How to start the out-of-process plugin host: ``python -m`` from a Python
    install, or the frozen executable's own ``--mira-plugin-host`` mode (the
    desktop build has no separate interpreter to run ``-m`` with)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--mira-plugin-host"]
    return [sys.executable, "-m", "mira_engine.plugins.host"]
