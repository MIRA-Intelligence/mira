"""What the engine hands a plugin when it builds the plugin's tools (design §3.6).

A tool entry in ``plugin.json`` may name

* a :class:`~mira_engine.agent.tools.base.Tool` subclass with a no-argument
  constructor (built once per agent loop),
* a Tool subclass whose ``__init__`` takes ``ctx`` (a :class:`PluginContext`), or
* a factory function ``(ctx) -> Tool``.

The context carries the agent loop's workspace and web proxy, the plugin's own
data directory, and a :class:`ConfigView`. Permissions shape it: a plugin that
isn't core-bundled only sees the config paths it declared in
``permissions.secrets`` and never the engine's ``host`` values.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def _norm(segment: str) -> str:
    return _CAMEL.sub("_", segment).replace("-", "_").lower()


def _split(path: str) -> list[str]:
    return [_norm(s) for s in path.split(".") if s]


def _walk(data: Any, segments: list[str]) -> Any:
    node = data
    for seg in segments:
        if not isinstance(node, Mapping):
            return None
        node = next((v for k, v in node.items() if _norm(str(k)) == seg), None)
        if node is None:
            return None
    return node


class ConfigView:
    """Read-only access to config values by dotted path (``channels.discord.token``;
    camelCase or snake_case segments). ``allowed`` limits it to those paths and
    their subtrees; ``None`` means everything (core-bundled plugins)."""

    def __init__(self, source: Callable[[], Mapping[str, Any] | None], allowed: tuple[str, ...] | None = None):
        self._source = source
        self._data: Mapping[str, Any] | None = None
        self.allowed = None if allowed is None else tuple(".".join(_split(p)) for p in allowed if p.strip())

    @classmethod
    def empty(cls) -> ConfigView:
        return cls(lambda: None, ())

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> ConfigView:
        """A view over ``{dotted path: value}`` (how a subprocess host receives
        its declared secrets)."""
        tree: dict[str, Any] = {}
        for path, value in values.items():
            node = tree
            parts = _split(path)
            for seg in parts[:-1]:
                node = node.setdefault(seg, {})
            if parts:
                node[parts[-1]] = value
        return cls(lambda: tree, tuple(values))

    def permits(self, path: str) -> bool:
        if self.allowed is None:
            return True
        wanted = ".".join(_split(path))
        return any(wanted == p or wanted.startswith(p + ".") for p in self.allowed)

    def get(self, path: str, default: Any = None) -> Any:
        if not self.permits(path):
            return default
        if self._data is None:
            self._data = self._source() or {}
        value = _walk(self._data, _split(path))
        return default if value is None else value

    def values(self) -> dict[str, Any]:
        """The declared paths and their values (for a restricted view)."""
        return {p: v for p in (self.allowed or ()) if (v := self.get(p)) is not None}


@dataclass(frozen=True)
class PluginContext:
    plugin_id: str
    tier: str
    workspace: Path | None = None
    proxy: str | None = None
    config: ConfigView = field(default_factory=ConfigView.empty)
    # Engine-provided values for core-bundled plugins (e.g. the loop's Brave
    # API key); always empty for any other tier.
    host: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    data_root: Path | None = None

    @property
    def data_dir(self) -> Path:
        """The plugin's private directory (``<data>/plugin-data/<id>``)."""
        root = self.data_root
        if root is None:
            from mira_engine.config.paths import get_data_dir

            root = get_data_dir() / "plugin-data" / self.plugin_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root


def accepts_ctx(cls: type) -> bool:
    try:
        return "ctx" in inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False


def instantiate(obj: object, base: type, entry: str, ctx: PluginContext | None = None) -> object:
    """Build a plugin object from its resolved entry (class, factory or instance)."""
    if inspect.isclass(obj):
        if not issubclass(obj, base):
            raise TypeError(f"{entry} is not a {base.__name__}")
        return obj(ctx=ctx) if accepts_ctx(obj) else obj()
    if isinstance(obj, base):
        return obj
    if callable(obj):
        made = obj(ctx)
        if not isinstance(made, base):
            raise TypeError(f"{entry} returned {type(made).__name__}, not a {base.__name__}")
        return made
    raise TypeError(f"{entry} is not a {base.__name__}")
