"""Persistent per-plugin runtime state: user enable/disable and tripped breakers.

Stored as JSON under the instance data dir (``plugins_state.json``), separate
from the skill-pack state so the two formats never collide. Never raises on a
missing or corrupt file — the kernel must start regardless.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

STATE_FILENAME = "plugins_state.json"


def default_state_path() -> Path:
    from mira_engine.config.paths import get_data_dir

    return get_data_dir() / STATE_FILENAME


class PluginState:
    def __init__(self, path: Path | None = None):
        self.path = path or default_state_path()
        self._data: dict[str, dict[str, Any]] = self._read()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("Could not persist plugin state: {}", e)

    def entry(self, plugin_id: str) -> dict[str, Any]:
        return dict(self._data.get(plugin_id) or {})

    def enabled_override(self, plugin_id: str) -> bool | None:
        value = self.entry(plugin_id).get("enabled")
        return value if isinstance(value, bool) else None

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        e = self._data.setdefault(plugin_id, {})
        e["enabled"] = enabled
        if enabled:
            # Re-enabling is the user's way to reset a tripped breaker.
            e.pop("tripped", None)
        self._write()

    def tripped(self, plugin_id: str) -> dict[str, Any] | None:
        value = self.entry(plugin_id).get("tripped")
        return value if isinstance(value, dict) else None

    def trip(self, plugin_id: str, reason: str) -> None:
        self._data.setdefault(plugin_id, {})["tripped"] = {"reason": reason, "at": int(time.time())}
        self._write()
