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
from mira_engine.plugins.catalog import plugin_dirs
from mira_engine.plugins.context import ConfigView, PluginContext, instantiate, plugin_data_dir
from mira_engine.plugins.guard import CircuitBreaker, GuardedHook, GuardedTool
from mira_engine.plugins.manifest import (
    ManifestError,
    PluginManifest,
    Tier,
    load_manifest,
)
from mira_engine.plugins.paths import bundled_plugins_dir, user_plugins_dir
from mira_engine.plugins.permissions import Grant, grant_for, granted
from mira_engine.plugins.state import PluginState

Status = Literal[
    "loaded", "disabled", "incompatible", "invalid", "failed", "tripped", "remote-disabled", "unsupported",
    "unavailable",
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
    # Channel entries (name -> "module:Class"), imported on first use by
    # PluginRuntime.channels() so loading never imports channel SDKs.
    channel_entries: dict[str, str] = field(default_factory=dict)
    tools: list[Tool] = field(default_factory=list)
    hooks: list[AgentHook] = field(default_factory=list)
    # In-process tool entries (resolved object, declared name, entry string),
    # built again for every agent loop by PluginRuntime.build_tools.
    tool_entries: list[tuple[object, str | None, str]] = field(default_factory=list)
    isolated: bool = False
    # What its in-process code runs under (plugins.permissions); None = unchecked.
    grant: Grant | None = None

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
        # As the import system does for a subpackage: reachable as an
        # attribute of its parent (``mira_plugins.telegram``).
        setattr(sys.modules[_PACKAGE_ROOT], name.rsplit(".", 1)[1], module)
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


def _run_granted(fn: Any, grant: Grant) -> Any:
    """``fn`` (sync or async) called under ``grant``."""
    if inspect.iscoroutinefunction(fn):
        async def call_async(*args: Any, **kwargs: Any) -> Any:
            with granted(grant):
                return await fn(*args, **kwargs)

        return call_async

    def call(*args: Any, **kwargs: Any) -> Any:
        with granted(grant):
            return fn(*args, **kwargs)

    return call


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


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
        audit_bundled: bool | None = None,
        data_root: Path | None = None,
    ):
        self.sources = sources if sources is not None else default_sources()
        self._audit_bundled = audit_bundled
        self.state = state or PluginState()
        # Config is read lazily — only when a user plugin is actually found —
        # so an engine without user plugins never touches it here.
        self._workspace = workspace
        self._allow_unsandboxed = allow_unsandboxed
        # Where plugins' data dirs live; None = <mira home>/plugin-data (the
        # contract kit points this at a temporary directory).
        self._data_root = data_root
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
            self._workspace = getattr(self._cfg(), "workspace_path", None)
        return self._workspace

    def _data_dir(self, plugin_id: str) -> Path | None:
        return self._data_root / plugin_id if self._data_root is not None else None

    @property
    def allow_unsandboxed(self) -> bool:
        if self._allow_unsandboxed is None:
            self._allow_unsandboxed = bool(getattr(getattr(self._cfg(), "plugins", None), "unsandboxed_experimental", False))
        return self._allow_unsandboxed

    @property
    def audit_bundled(self) -> bool:
        """Check first-party plugins too, logging only (``MIRA_PLUGIN_AUDIT=1``);
        an environment switch so loading bundled plugins never reads the config."""
        if self._audit_bundled is None:
            self._audit_bundled = os.environ.get("MIRA_PLUGIN_AUDIT", "").lower() in ("1", "true", "yes")
        return self._audit_bundled

    def grant(self, manifest: PluginManifest, tier: Tier | None, workspace: Path | None = None) -> Grant | None:
        if tier == "core-bundled" and not self.audit_bundled:
            return None
        # The configured workspace when the caller has none: a load-time grant
        # (hooks, channels) must cover it as a per-loop one does.
        return grant_for(
            manifest, tier, workspace=workspace or self.workspace, audit_bundled=self.audit_bundled,
            data_dir=self._data_dir(manifest.id),
        )

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
            data_root=self._data_dir(manifest.id),
        )

    # -- discovery ------------------------------------------------------------

    def discover(self) -> list[tuple[Path, Tier]]:
        found: list[tuple[Path, Tier]] = []
        for source in self.sources:
            dirs = plugin_dirs(source.path)
            if dirs:
                tier = self._source_tier(source)
                found.extend((d, tier) for d in dirs)
        return found

    # -- loading --------------------------------------------------------------

    def load(self) -> list[LoadedPlugin]:
        self.plugins = []
        seen: set[str] = set()
        for plugin_dir, source_tier in self.discover():
            loaded = self._load_one(plugin_dir, source_tier, seen)
            self.plugins.append(loaded)
            if loaded.status == "loaded":
                # First-party plugins load on every start; only user plugins are news.
                log = logger.debug if loaded.tier == "core-bundled" else logger.info
                log("plugin {} {} loaded ({})", loaded.id, loaded.manifest.version, loaded.tier)
            elif loaded.status == "unavailable":
                logger.debug("plugin {} unavailable: {}", loaded.id, loaded.reason)
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
        missing = [m for m in manifest.requires if not _importable(m)]
        if missing:
            result.status, result.reason = "unavailable", f"needs {', '.join(missing)} (not installed)"
            return result
        if manifest.has_code and (tier == "experimental" or manifest.isolation == "subprocess"):
            return self._load_isolated(result, manifest)
        if tier not in _TRUSTED and any(h.mode == "mutate" for h in manifest.hooks):
            result.reason = f"mutate hooks need a verified or core-bundled plugin (this one is {tier})"
            return result
        if tier not in _TRUSTED and manifest.providers:
            # A provider sees every prompt and holds the user's model keys.
            result.reason = f"LLM providers need a verified or core-bundled plugin (this one is {tier})"
            return result

        breaker = self.breaker(manifest.id, manifest.version, tier=tier)
        # Built once here to validate the entries (and for status and the
        # contract kit); every agent loop builds its own via build_tools().
        # Hooks keep this context, so a user plugin's hooks see the configured
        # workspace (its config is read anyway; bundled plugins load without).
        probe = self.context(manifest, tier, workspace=self._workspace if tier == "core-bundled" else self.workspace)
        grant = result.grant = self.grant(manifest, tier)
        try:
            with granted(grant):  # the plugin's import and construction run as it
                self._build_entries(result, manifest, breaker, probe, grant)
        except Exception as e:  # noqa: BLE001 - any plugin error stays with the plugin
            result.channels, result.channel_entries, result.tools, result.hooks, result.tool_entries = {}, {}, [], [], []
            result.reason = f"{type(e).__name__}: {e}"
            return result

        result.status = "loaded"
        return result

    def _build_entries(
        self,
        result: LoadedPlugin,
        manifest: PluginManifest,
        breaker: CircuitBreaker,
        probe: PluginContext,
        grant: Grant | None,
    ) -> None:
        for ep in manifest.channels:
            result.channel_entries[ep.name or ep.entry.split(":", 1)[1]] = ep.entry
        for ep in manifest.tools:
            obj = _resolve(manifest, ep.entry)
            tool = _build_tool(obj, ep.name, ep.entry, probe)
            result.tools.append(GuardedTool(tool, breaker, grant=grant))  # type: ignore[arg-type]
            result.tool_entries.append((obj, ep.name, ep.entry))
        for ep in manifest.hooks:
            hook = _instantiate(_resolve(manifest, ep.entry), AgentHook, ep.entry, probe)
            result.hooks.append(GuardedHook(hook, breaker, mode=ep.mode, grant=grant))  # type: ignore[arg-type]

    def _load_isolated(self, result: LoadedPlugin, manifest: PluginManifest) -> LoadedPlugin:
        """Serve the plugin's tools from a separate (sandboxed) process (mira#211)."""
        from mira_engine.plugins import isolation

        if manifest.channels or manifest.hooks or manifest.providers:
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
            manifest.id,
            isolation.host_argv(manifest, workspace=self.workspace, sandbox=sandbox),
            env=env,
            cwd=plugin_data_dir(manifest.id),
        )
        breaker = self.breaker(manifest.id, manifest.version, tier=result.tier)
        result.tools = [GuardedTool(isolation.SubprocessTool(client, d), breaker) for d in definitions]
        result.isolated = True
        result.status = "loaded"
        result.reason = f"out of process ({sandbox or 'unsandboxed'})"
        return result

    # -- views ----------------------------------------------------------------

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return next((p for p in self.plugins if p.id == plugin_id), None)

    def loaded(self) -> list[LoadedPlugin]:
        return [p for p in self.plugins if p.status == "loaded"]

    def resolve_channels(self, plugin: LoadedPlugin) -> dict[str, type]:
        """Import the plugin's channel classes (once). A channel whose class
        can't be imported is left out and logged; the plugin's others stay."""
        from mira_engine.channels.base import BaseChannel

        assert plugin.manifest is not None
        for name, entry in plugin.channel_entries.items():
            if name in plugin.channels:
                continue
            try:
                with granted(plugin.grant):
                    cls = _resolve(plugin.manifest, entry)
                if not (inspect.isclass(cls) and issubclass(cls, BaseChannel)):
                    raise TypeError(f"{entry} is not a BaseChannel subclass")
            except Exception as e:  # noqa: BLE001 - the plugin's problem, not the engine's
                logger.warning("plugin {} channel {} not available: {}: {}", plugin.id, name, type(e).__name__, e)
                plugin.reason = f"channel {name}: {type(e).__name__}: {e}"
                continue
            cls.__mira_plugin_id__ = plugin.id  # type: ignore[attr-defined]
            plugin.channels[name] = cls
        return plugin.channels

    def provider_hook(self, name: str, hook: str = "entry") -> Any:
        """A loaded plugin's provider hook (``entry``, ``login``, ``login_status``,
        ``models``) for provider ``name``, imported on first use; None when no
        loaded plugin provides it or the hook isn't declared. The hook runs
        under the plugin's grant (``plugins.permissions``)."""
        for p in self.loaded():
            assert p.manifest is not None
            for entry in p.manifest.providers:
                if entry.name != name:
                    continue
                target = getattr(entry, hook, None)
                if not target:
                    return None
                with granted(p.grant):
                    fn = _resolve(p.manifest, target)
                if p.grant is None:
                    return fn
                return _run_granted(fn, p.grant)
        return None

    def channel_grant(self, cls: type) -> Grant | None:
        """What a plugin channel's code runs under (None for the engine's own)."""
        plugin = self.get(getattr(cls, "__mira_plugin_id__", "") or "")
        return plugin.grant if plugin is not None else None

    def channels(self, *, core: bool | None = None) -> dict[str, type]:
        """Channel classes from loaded plugins, imported on first use.
        ``core``: only core-bundled (True) or only other tiers (False)."""
        out: dict[str, type] = {}
        for p in self.loaded():
            if core is not None and (p.tier == "core-bundled") != core:
                continue
            for name, cls in self.resolve_channels(p).items():
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
            grant = self.grant(p.manifest, p.tier, workspace)
            for obj, name, entry in p.tool_entries:
                try:
                    with granted(grant):
                        tool = _build_tool(obj, name, entry, ctx)
                    out.append(GuardedTool(tool, breaker, grant=grant))  # type: ignore[arg-type]
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
