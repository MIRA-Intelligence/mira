"""Fault-tolerant plugin loader (design §3.6, mira#206).

One loader for every kind of extension, used for first-party ("bundled")
plugins and user-installed ones alike, so the plugin contract is exercised by
mira's own code and community plugins get the same capabilities.

Guarantees:

* **Isolation of failure** — a plugin whose manifest is invalid, whose API
  range does not match, whose import raises, or whose entry points are wrong
  is recorded with a status and reason; nothing else is affected and the
  engine always starts.
* **Trust is not self-declared** — bundled plugins are ``core-bundled``; a
  user-installed plugin is at most ``community`` whatever its manifest says
  (``verified`` comes from the platform registry later). ``experimental``
  plugins and ``isolation: subprocess`` need the out-of-process runner (P5)
  and are not loaded in-process.
* **Mutating hooks need trust** — only ``core-bundled``/``verified`` plugins
  may register ``mode: mutate`` hooks.
* **Guarded execution** — tools and hooks are wrapped in the plugin's
  :class:`~mira_engine.plugins.guard.CircuitBreaker`; a plugin that keeps
  failing is tripped and stays off until the user re-enables it.
* **User control** — ``enabled`` overrides in :class:`PluginState`; community
  plugins are off unless enabled (or ``default_enabled``).
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import inspect
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from loguru import logger

from mira_engine.agent.hook import AgentHook
from mira_engine.agent.tools.base import Tool
from mira_engine.plugins.api import PLUGIN_API_VERSION, satisfies
from mira_engine.plugins.guard import CircuitBreaker, GuardedHook, GuardedTool
from mira_engine.plugins.manifest import MANIFEST_FILENAME, ManifestError, PluginManifest, Tier, load_manifest
from mira_engine.plugins.state import PluginState

Status = Literal["loaded", "disabled", "incompatible", "invalid", "failed", "tripped", "unsupported"]
_TRUSTED: tuple[Tier, ...] = ("core-bundled", "verified")
_PACKAGE_ROOT = "mira_plugins"


@dataclass
class LoadedPlugin:
    dir: Path
    status: Status
    reason: str = ""
    manifest: PluginManifest | None = None
    tier: Tier | None = None
    channels: dict[str, type] = field(default_factory=dict)
    tools: list[Tool] = field(default_factory=list)
    hooks: list[AgentHook] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.manifest.id if self.manifest else self.dir.name


@dataclass(frozen=True)
class PluginSource:
    """A directory of plugin directories, and the tier its plugins get."""

    path: Path
    tier: Tier


def bundled_plugins_dir() -> Path | None:
    """First-party plugins: shipped as ``mira_engine/_bundled_plugins`` in the
    wheel, or the repo's ``plugins/`` in a source checkout."""
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


def default_sources() -> list[PluginSource]:
    sources: list[PluginSource] = []
    bundled = bundled_plugins_dir()
    if bundled:
        sources.append(PluginSource(bundled, "core-bundled"))
    sources.append(PluginSource(user_plugins_dir(), "community"))
    return sources


def _effective_tier(manifest: PluginManifest, source_tier: Tier) -> Tier:
    if source_tier == "core-bundled":
        return "core-bundled"
    # Self-declared trust is ignored; only "experimental" (less trust) is honoured.
    return "experimental" if manifest.declared_tier == "experimental" else "community"


def _ensure_namespace() -> None:
    if _PACKAGE_ROOT not in sys.modules:
        ns = types.ModuleType(_PACKAGE_ROOT)
        ns.__path__ = []  # type: ignore[attr-defined]
        sys.modules[_PACKAGE_ROOT] = ns


def _package_for(manifest: PluginManifest) -> str:
    """Import the plugin directory as its own package (relative imports work)."""
    _ensure_namespace()
    name = f"{_PACKAGE_ROOT}.{manifest.id.replace('-', '_')}"
    if name not in sys.modules:
        init = manifest.root / "__init__.py"
        spec = (
            importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(manifest.root)])
            if init.is_file()
            else importlib.machinery.ModuleSpec(name, None, is_package=True)
        )
        if spec is None:
            raise ImportError(f"cannot create a package for {manifest.root}")
        if spec.submodule_search_locations is not None and not init.is_file():
            spec.submodule_search_locations.append(str(manifest.root))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        if spec.loader:
            spec.loader.exec_module(module)
    return name


def _resolve(manifest: PluginManifest, entry: str) -> object:
    module_name, attr = entry.split(":", 1)
    module = importlib.import_module(f"{_package_for(manifest)}.{module_name}")
    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise ImportError(f"{entry}: no attribute {attr!r}") from e


def _instantiate(obj: object, base: type, entry: str) -> object:
    if inspect.isclass(obj):
        if not issubclass(obj, base):
            raise TypeError(f"{entry} is not a {base.__name__}")
        return obj()
    if isinstance(obj, base):
        return obj
    raise TypeError(f"{entry} is not a {base.__name__}")


class PluginRuntime:
    """Discovers, loads and holds every plugin for this process."""

    def __init__(self, sources: list[PluginSource] | None = None, state: PluginState | None = None):
        self.sources = sources if sources is not None else default_sources()
        self.state = state or PluginState()
        self.plugins: list[LoadedPlugin] = []
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, plugin_id: str) -> CircuitBreaker:
        if plugin_id not in self._breakers:
            self._breakers[plugin_id] = CircuitBreaker(plugin_id, self.state)
        return self._breakers[plugin_id]

    # -- discovery ------------------------------------------------------------

    def discover(self) -> list[tuple[Path, Tier]]:
        found: list[tuple[Path, Tier]] = []
        for source in self.sources:
            try:
                children = sorted(p for p in source.path.iterdir() if p.is_dir())
            except OSError:
                continue
            found.extend((d, source.tier) for d in children if (d / MANIFEST_FILENAME).is_file())
        return found

    # -- loading --------------------------------------------------------------

    def load(self) -> list[LoadedPlugin]:
        self.plugins = []
        seen: set[str] = set()
        for plugin_dir, source_tier in self.discover():
            loaded = self._load_one(plugin_dir, source_tier, seen)
            self.plugins.append(loaded)
            if loaded.status == "loaded":
                logger.info("plugin {} {} loaded ({})", loaded.id, loaded.manifest.version, loaded.tier)
            elif loaded.status not in ("disabled",):
                logger.warning("plugin {} not loaded [{}]: {}", loaded.id, loaded.status, loaded.reason)
        return self.plugins

    def _load_one(self, plugin_dir: Path, source_tier: Tier, seen: set[str]) -> LoadedPlugin:
        try:
            manifest = load_manifest(plugin_dir)
        except ManifestError as e:
            return LoadedPlugin(plugin_dir, "invalid", str(e))
        tier = _effective_tier(manifest, source_tier)
        result = LoadedPlugin(plugin_dir, "failed", manifest=manifest, tier=tier)

        if manifest.id in seen:
            result.status, result.reason = "invalid", "duplicate plugin id (an earlier source wins)"
            return result
        seen.add(manifest.id)
        if not satisfies(manifest.plugin_api):
            result.status = "incompatible"
            result.reason = f"needs plugin API {manifest.plugin_api}; this engine provides {PLUGIN_API_VERSION}"
            return result
        tripped = self.state.tripped(manifest.id)
        if tripped:
            result.status = "tripped"
            result.reason = f"disabled after repeated failures ({tripped.get('reason')}); re-enable to retry"
            return result
        override = self.state.enabled_override(manifest.id)
        enabled = override if override is not None else (tier == "core-bundled" or manifest.default_enabled)
        if not enabled:
            result.status, result.reason = "disabled", "not enabled (mira plugin enable <id>)"
            return result
        if manifest.has_code and (tier == "experimental" or manifest.isolation == "subprocess"):
            result.status = "unsupported"
            result.reason = "experimental / subprocess plugins need the out-of-process runner (not available yet)"
            return result
        if tier not in _TRUSTED and any(h.mode == "mutate" for h in manifest.hooks):
            result.reason = f"mutate hooks need a verified or core-bundled plugin (this one is {tier})"
            return result

        breaker = self.breaker(manifest.id)
        try:
            for ep in manifest.channels:
                from mira_engine.channels.base import BaseChannel

                cls = _resolve(manifest, ep.entry)
                if not (inspect.isclass(cls) and issubclass(cls, BaseChannel)):
                    raise TypeError(f"{ep.entry} is not a BaseChannel subclass")
                result.channels[ep.name or cls.name] = cls
            for ep in manifest.tools:
                tool = _instantiate(_resolve(manifest, ep.entry), Tool, ep.entry)
                if ep.name and tool.name != ep.name:  # type: ignore[attr-defined]
                    raise ValueError(f"{ep.entry} is named {tool.name!r}, manifest says {ep.name!r}")  # type: ignore[attr-defined]
                result.tools.append(GuardedTool(tool, breaker))  # type: ignore[arg-type]
            for ep in manifest.hooks:
                hook = _instantiate(_resolve(manifest, ep.entry), AgentHook, ep.entry)
                result.hooks.append(GuardedHook(hook, breaker, mode=ep.mode))  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 - any plugin error stays with the plugin
            result.channels, result.tools, result.hooks = {}, [], []
            result.reason = f"{type(e).__name__}: {e}"
            return result

        result.status = "loaded"
        return result

    # -- views ----------------------------------------------------------------

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return next((p for p in self.plugins if p.id == plugin_id), None)

    def loaded(self) -> list[LoadedPlugin]:
        return [p for p in self.plugins if p.status == "loaded"]

    def channels(self) -> dict[str, type]:
        out: dict[str, type] = {}
        for p in self.loaded():
            for name, cls in p.channels.items():
                out.setdefault(name, cls)
        return out

    def tools(self) -> list[Tool]:
        return [t for p in self.loaded() for t in p.tools]

    def hooks(self) -> list[AgentHook]:
        return [h for p in self.loaded() for h in p.hooks]


_runtime: PluginRuntime | None = None


def get_runtime() -> PluginRuntime:
    """The process-wide plugin runtime, loaded on first use. Never raises."""
    global _runtime
    if _runtime is None:
        try:
            runtime = PluginRuntime()
            runtime.load()
        except Exception as e:  # noqa: BLE001 - the engine must start without plugins
            logger.error("plugin runtime failed to initialise; continuing without plugins: {}", e)
            runtime = PluginRuntime(sources=[], state=PluginState(Path(os.devnull)))
        _runtime = runtime
    return _runtime


def set_runtime_for_tests(runtime: PluginRuntime | None) -> None:
    global _runtime
    _runtime = runtime
