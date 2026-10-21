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
  user-installed plugin gets the tier the platform registry issued for it,
  else the source's default (``experimental`` for ``~/.mira/plugins``),
  whatever its manifest says. ``experimental`` plugins and
  ``isolation: subprocess`` run out of process (:mod:`.isolation`) and may
  only provide tools and skills.
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
from typing import Any, Literal

from loguru import logger

from mira_engine.agent.hook import AgentHook
from mira_engine.agent.tools.base import Tool
from mira_engine.plugins.api import PLUGIN_API_VERSION, satisfies
from mira_engine.plugins.context import ConfigView, PluginContext, instantiate
from mira_engine.plugins.guard import CircuitBreaker, GuardedHook, GuardedTool
from mira_engine.plugins.manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    PluginManifest,
    Tier,
    load_manifest,
)
from mira_engine.plugins.paths import bundled_plugins_dir, user_plugins_dir
from mira_engine.plugins.state import PluginState

Status = Literal[
    "loaded", "disabled", "incompatible", "invalid", "failed", "tripped", "remote-disabled", "unsupported"
]
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
    # In-process tool entries (resolved object, declared name, entry string),
    # built again for every agent loop by PluginRuntime.build_tools.
    tool_entries: list[tuple[object, str | None, str]] = field(default_factory=list)
    isolated: bool = False

    @property
    def id(self) -> str:
        return self.manifest.id if self.manifest else self.dir.name


@dataclass(frozen=True)
class PluginSource:
    """A directory of plugin directories, and the tier its plugins get.
    ``from_config``: take the tier from ``plugins.default_user_tier`` instead
    (read only once a plugin is actually found there)."""

    path: Path
    tier: Tier
    from_config: bool = False


_UNSET = object()


def _load_config_or_none() -> Any:
    try:
        from mira_engine.config.loader import load_config

        return load_config()
    except Exception:  # noqa: BLE001 - defaults when config can't be read
        return None


def default_sources() -> list[PluginSource]:
    sources: list[PluginSource] = []
    bundled = bundled_plugins_dir()
    if bundled:
        sources.append(PluginSource(bundled, "core-bundled"))
    sources.append(PluginSource(user_plugins_dir(), "experimental", from_config=True))
    return sources


def _effective_tier(manifest: PluginManifest, source_tier: Tier, registry_tier: str | None = None) -> Tier:
    if source_tier == "core-bundled":
        return "core-bundled"
    # The platform registry decides (mira#211); a manifest can only ask for less trust.
    if registry_tier in ("verified", "community", "experimental"):
        return registry_tier  # type: ignore[return-value]
    return "experimental" if manifest.declared_tier == "experimental" else source_tier


def _ensure_namespace() -> None:
    if _PACKAGE_ROOT not in sys.modules:
        ns = types.ModuleType(_PACKAGE_ROOT)
        ns.__path__ = []  # type: ignore[attr-defined]
        sys.modules[_PACKAGE_ROOT] = ns


def _package_for(manifest: PluginManifest) -> str:
    """Import the plugin directory as its own package (relative imports work)."""
    _ensure_namespace()
    name = f"{_PACKAGE_ROOT}.{manifest.id.replace('-', '_')}"
    cached = sys.modules.get(name)
    if cached is not None and str(manifest.root) not in list(getattr(cached, "__path__", None) or []):
        # Same id from another directory (another source, a reinstall, a test
        # fixture): drop the stale package and its submodules.
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]
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


def _instantiate(obj: object, base: type, entry: str, ctx: PluginContext | None = None) -> object:
    return instantiate(obj, base, entry, ctx)


def _build_tool(obj: object, name: str | None, entry: str, ctx: PluginContext) -> Tool:
    tool = _instantiate(obj, Tool, entry, ctx)
    if name and tool.name != name:  # type: ignore[attr-defined]
        raise ValueError(f"{entry} is named {tool.name!r}, manifest says {name!r}")  # type: ignore[attr-defined]
    return tool  # type: ignore[return-value]


class PluginRuntime:
    """Discovers, loads and holds every plugin for this process."""

    def __init__(
        self,
        sources: list[PluginSource] | None = None,
        state: PluginState | None = None,
        *,
        workspace: Path | None = None,
        allow_unsandboxed: bool | None = None,
    ):
        self.sources = sources if sources is not None else default_sources()
        self.state = state or PluginState()
        # Config is read lazily — only when a user plugin is actually found —
        # so an engine without user plugins never touches it here.
        self._workspace = workspace
        self._allow_unsandboxed = allow_unsandboxed
        self._config: Any = _UNSET
        self.plugins: list[LoadedPlugin] = []
        self._breakers: dict[str, CircuitBreaker] = {}

    def _cfg(self) -> Any:
        if self._config is _UNSET:
            self._config = _load_config_or_none()
        return self._config

    @property
    def workspace(self) -> Path | None:
        if self._workspace is None and self._cfg() is not None:
            self._workspace = self._cfg().workspace_path
        return self._workspace

    @property
    def allow_unsandboxed(self) -> bool:
        if self._allow_unsandboxed is None:
            self._allow_unsandboxed = bool(getattr(getattr(self._cfg(), "plugins", None), "unsandboxed_experimental", False))
        return self._allow_unsandboxed

    def _source_tier(self, source: PluginSource) -> Tier:
        if not source.from_config:
            return source.tier
        tier = getattr(getattr(self._cfg(), "plugins", None), "default_user_tier", source.tier)
        return tier if tier in ("community", "experimental") else source.tier

    def breaker(self, plugin_id: str, version: str | None = None, *, tier: Tier | None = None) -> CircuitBreaker:
        if plugin_id not in self._breakers:
            # A first-party plugin that trips (e.g. web search timing out) is
            # paused for a while, not switched off for good.
            self._breakers[plugin_id] = CircuitBreaker(
                plugin_id, self.state, version=version, persist=tier != "core-bundled"
            )
        return self._breakers[plugin_id]

    # -- plugin context -------------------------------------------------------

    def _config_data(self) -> Any:
        cfg = self._cfg()
        try:
            return cfg.model_dump() if cfg is not None else None
        except Exception:  # noqa: BLE001 - a plugin gets no config rather than breaking
            return None

    def config_view(self, manifest: PluginManifest, tier: Tier | None) -> ConfigView:
        allowed = None if tier == "core-bundled" else manifest.permissions.secrets
        return ConfigView(self._config_data, allowed)

    def context(
        self,
        manifest: PluginManifest,
        tier: Tier | None,
        *,
        workspace: Path | None = None,
        proxy: str | None = None,
        host: dict[str, Any] | None = None,
    ) -> PluginContext:
        trusted = tier == "core-bundled"
        return PluginContext(
            plugin_id=manifest.id,
            tier=tier or "experimental",
            workspace=workspace,
            proxy=proxy,
            config=self.config_view(manifest, tier),
            host=dict(host or {}) if trusted else {},
        )

    # -- discovery ------------------------------------------------------------

    def discover(self) -> list[tuple[Path, Tier]]:
        found: list[tuple[Path, Tier]] = []
        for source in self.sources:
            try:
                children = sorted(p for p in source.path.iterdir() if p.is_dir())
            except OSError:
                continue
            plugin_dirs = [d for d in children if (d / MANIFEST_FILENAME).is_file()]
            if plugin_dirs:
                tier = self._source_tier(source)
                found.extend((d, tier) for d in plugin_dirs)
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
        tier = _effective_tier(manifest, source_tier, self.state.registry_tier(manifest.id) if source_tier != "core-bundled" else None)
        result = LoadedPlugin(plugin_dir, "failed", manifest=manifest, tier=tier)

        if manifest.id in seen:
            result.status, result.reason = "invalid", "duplicate plugin id (an earlier source wins)"
            return result
        seen.add(manifest.id)
        if not satisfies(manifest.plugin_api):
            result.status = "incompatible"
            result.reason = f"needs plugin API {manifest.plugin_api}; this engine provides {PLUGIN_API_VERSION}"
            return result
        remote = self.state.remote_reason(manifest.id, manifest.version)
        if remote:
            result.status = "remote-disabled"
            result.reason = f"disabled by the plugin registry: {remote}"
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
            return self._load_isolated(result, manifest)
        if tier not in _TRUSTED and any(h.mode == "mutate" for h in manifest.hooks):
            result.reason = f"mutate hooks need a verified or core-bundled plugin (this one is {tier})"
            return result

        breaker = self.breaker(manifest.id, manifest.version, tier=tier)
        # Built once here to validate the entries (and for status and the
        # contract kit); every agent loop builds its own via build_tools().
        probe = self.context(manifest, tier, workspace=self._workspace)
        try:
            for ep in manifest.channels:
                from mira_engine.channels.base import BaseChannel

                cls = _resolve(manifest, ep.entry)
                if not (inspect.isclass(cls) and issubclass(cls, BaseChannel)):
                    raise TypeError(f"{ep.entry} is not a BaseChannel subclass")
                result.channels[ep.name or cls.name] = cls
            for ep in manifest.tools:
                obj = _resolve(manifest, ep.entry)
                tool = _build_tool(obj, ep.name, ep.entry, probe)
                result.tools.append(GuardedTool(tool, breaker))  # type: ignore[arg-type]
                result.tool_entries.append((obj, ep.name, ep.entry))
            for ep in manifest.hooks:
                hook = _instantiate(_resolve(manifest, ep.entry), AgentHook, ep.entry, probe)
                result.hooks.append(GuardedHook(hook, breaker, mode=ep.mode))  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 - any plugin error stays with the plugin
            result.channels, result.tools, result.hooks, result.tool_entries = {}, [], [], []
            result.reason = f"{type(e).__name__}: {e}"
            return result

        result.status = "loaded"
        return result

    def _load_isolated(self, result: LoadedPlugin, manifest: PluginManifest) -> LoadedPlugin:
        """Serve the plugin's tools from a separate (sandboxed) process (mira#211)."""
        from mira_engine.plugins import isolation

        if manifest.channels or manifest.hooks:
            result.reason = "out-of-process plugins may only provide tools and skills"
            return result
        try:
            sandbox = isolation.plan(manifest, workspace=self.workspace, allow_unsandboxed=self.allow_unsandboxed)
        except isolation.IsolationUnavailable as e:
            result.status, result.reason = "unsupported", str(e)
            return result
        env = isolation.plugin_env(
            manifest,
            workspace=self.workspace,
            config=self.config_view(manifest, result.tier).values(),
            tier=result.tier,
        )
        try:
            definitions = isolation.describe(manifest, workspace=self.workspace, sandbox=sandbox, env=env)
        except Exception as e:  # noqa: BLE001 - the plugin's problem, not the engine's
            result.reason = f"{type(e).__name__}: {e}"
            return result
        declared = {ep.name for ep in manifest.tools}
        served = {str(d.get("name")) for d in definitions}
        if declared != served:
            result.reason = f"manifest declares tools {sorted(declared)} but the plugin serves {sorted(served)}"
            return result
        client = isolation.PluginHostClient(
            manifest.id, isolation.host_argv(manifest, workspace=self.workspace, sandbox=sandbox), env=env
        )
        breaker = self.breaker(manifest.id, manifest.version, tier=result.tier)
        result.tools = [GuardedTool(isolation.SubprocessTool(client, d), breaker) for d in definitions]
        result.isolated = True
        result.status = "loaded"
        result.reason = "out of process" + ("" if sandbox else " (unsandboxed)")
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
        """The tools built at load time (status, contract checks, tests)."""
        return [t for p in self.loaded() for t in p.tools]

    def build_tools(
        self,
        *,
        workspace: Path | None = None,
        proxy: str | None = None,
        host: dict[str, Any] | None = None,
    ) -> list[Tool]:
        """Fresh tool instances for one agent loop (its workspace and proxy),
        behind the plugin's shared breaker. Out-of-process plugins keep their
        single host, so their load-time tools are reused. A tool whose
        construction fails is left out and logged; the rest still load."""
        out: list[Tool] = []
        for p in self.loaded():
            if p.isolated or not p.tool_entries:
                out.extend(p.tools)
                continue
            assert p.manifest is not None
            ctx = self.context(p.manifest, p.tier, workspace=workspace, proxy=proxy, host=host)
            breaker = self.breaker(p.manifest.id, p.manifest.version, tier=p.tier)
            for obj, name, entry in p.tool_entries:
                try:
                    out.append(GuardedTool(_build_tool(obj, name, entry, ctx), breaker))  # type: ignore[arg-type]
                except Exception as e:  # noqa: BLE001 - the plugin's problem, not the loop's
                    logger.warning("plugin {} tool {} not available here: {}", p.id, name or entry, e)
        return out

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
