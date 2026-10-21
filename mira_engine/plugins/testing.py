"""Plugin contract test kit (design §3.7, mira#208).

The checks every plugin must pass, maintained by the kernel — so the community
CI for a plugin is "run the contract", and a plugin PR cannot weaken it.

Use from a plugin's own tests::

    from mira_engine.plugins.testing import assert_plugin_contract

    def test_contract():
        assert_plugin_contract(Path(__file__).parents[1])

or from the CLI: ``mira plugin test <plugin-dir>``.

The checks are deliberately side-effect free: channels are constructed and
stopped but never started, tools are inspected but never executed, hooks are
called on a throwaway context.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mira_engine.agent.hook import AgentHook, AgentHookContext
from mira_engine.agent.tools.base import Tool
from mira_engine.plugins.api import PLUGIN_API_VERSION, satisfies
from mira_engine.plugins.guard import GuardedHook, GuardedTool
from mira_engine.plugins.loader import PluginRuntime
from mira_engine.plugins.manifest import ManifestError, PluginManifest, Tier, load_manifest
from mira_engine.plugins.state import PluginState

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CHANNEL_METHODS = ("start", "stop", "send")
_HOOK_METHODS = ("before_iteration", "on_stream", "on_stream_end", "before_execute_tools", "after_iteration")


@dataclass
class ContractReport:
    plugin_id: str
    passed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def check(self, label: str, problems: list[str]) -> None:
        if problems:
            self.problems.extend(f"{label}: {p}" for p in problems)
        else:
            self.passed.append(label)


def _run(coro: Any, timeout: float) -> None:
    async def bounded() -> None:
        await asyncio.wait_for(coro, timeout)

    asyncio.run(bounded())


def check_tool(tool: Tool) -> list[str]:
    problems: list[str] = []
    if isinstance(tool, GuardedTool):
        tool = tool._inner
    if not _TOOL_NAME.match(str(tool.name or "")):
        problems.append(f"name {tool.name!r} must match {_TOOL_NAME.pattern}")
    if not (isinstance(tool.description, str) and 0 < len(tool.description) <= 4000):
        problems.append("description must be a non-empty string (<= 4000 chars)")
    params = tool.parameters
    if not isinstance(params, dict) or params.get("type", "object") != "object":
        problems.append("parameters must be a JSON schema object")
    elif not isinstance(params.get("properties", {}), dict):
        problems.append("parameters.properties must be an object")
    if not inspect.iscoroutinefunction(type(tool).execute):
        problems.append("execute must be async")
    try:
        json.dumps(tool.to_schema())
        tool.validate_params({})
    except Exception as e:  # noqa: BLE001
        problems.append(f"schema is not usable: {e}")
    return problems


def _channel_config(name: str, cls: type | None = None) -> Any:
    """The channel's real config defaults: mira's config section for it, else
    the channel's own ``default_config()``."""
    try:
        from mira_engine.config.schema import ChannelsConfig

        section = getattr(ChannelsConfig(), name, None)
        if section is not None:
            from mira_engine.channels.manager import ChannelManager

            ns = ChannelManager._to_ns(section)
            ns.enabled = True
            ns.allow_from = ["*"]
            return ns
    except Exception:  # noqa: BLE001 - fall back to the channel's defaults
        pass
    try:
        defaults = cls.default_config() if cls is not None else None
    except Exception:  # noqa: BLE001
        defaults = None
    if isinstance(defaults, dict) and defaults:
        return {**defaults, "enabled": True, "allowFrom": ["*"]}
    return SimpleNamespace(enabled=True, allow_from=["*"])


def check_channel(cls: type, name: str) -> list[str]:
    from mira_engine.bus.queue import MessageBus
    from mira_engine.channels.base import BaseChannel

    problems: list[str] = []
    if not (inspect.isclass(cls) and issubclass(cls, BaseChannel)):
        return ["must subclass BaseChannel"]
    if getattr(cls, "name", None) != name:
        problems.append(f"class attribute name is {getattr(cls, 'name', None)!r}, manifest says {name!r}")
    for method in _CHANNEL_METHODS:
        if not inspect.iscoroutinefunction(getattr(cls, method, None)):
            problems.append(f"{method}() must be async")
    if problems:
        return problems
    try:
        channel = cls(_channel_config(name, cls), MessageBus())
    except Exception as e:  # noqa: BLE001
        return [f"constructing with default config raised {type(e).__name__}: {e}"]
    try:
        _run(channel.stop(), 5.0)
    except asyncio.TimeoutError:
        problems.append("stop() before start() did not return within 5s")
    except Exception as e:  # noqa: BLE001
        problems.append(f"stop() before start() raised {type(e).__name__}: {e}")
    if not callable(getattr(channel, "is_allowed", None)):
        problems.append("is_allowed() is missing")
    return problems


def check_hook(hook: AgentHook) -> list[str]:
    inner = hook._inner if isinstance(hook, GuardedHook) else hook
    problems: list[str] = []
    for method in _HOOK_METHODS:
        if not inspect.iscoroutinefunction(getattr(type(inner), method, None)):
            problems.append(f"{method}() must be async")
    if problems:
        return problems
    ctx = AgentHookContext(iteration=0, messages=[{"role": "user", "content": "contract check"}])
    try:
        _run(inner.before_iteration(ctx.snapshot()), 2.0)
        _run(inner.after_iteration(ctx.snapshot()), 2.0)
    except asyncio.TimeoutError:
        problems.append("a hook call took longer than 2s")
    except Exception as e:  # noqa: BLE001
        problems.append(f"raised {type(e).__name__}: {e}")
    return problems


def check_skills(manifest: PluginManifest) -> list[str]:
    """Each provided skill is a directory inside the plugin with a SKILL.md."""
    problems: list[str] = []
    root = manifest.root.resolve()
    for i, skill in enumerate(manifest.skills):
        sid = skill.get("id")
        rel = skill.get("path", sid)
        if not isinstance(sid, str) or not sid:
            problems.append(f"skill #{i + 1} needs an id")
            continue
        if not isinstance(rel, str) or not rel.strip():
            problems.append(f"skill {sid} needs a path")
            continue
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            problems.append(f"skill {sid}: {rel} is outside the plugin")
        elif not ((target / "SKILL.md").is_file() or (target.name == "SKILL.md" and target.is_file())):
            problems.append(f"skill {sid}: no SKILL.md at {rel}")
    return problems


def check_plugin(plugin_dir: str | Path, tier: Tier = "community") -> ContractReport:
    """Run the full contract for the plugin in ``plugin_dir``."""
    plugin_dir = Path(plugin_dir).resolve()
    try:
        manifest = load_manifest(plugin_dir)
    except ManifestError as e:
        report = ContractReport(plugin_dir.name)
        report.check("manifest", [str(e)])
        return report
    report = ContractReport(manifest.id)
    report.check("manifest", [])
    report.check(
        "plugin_api",
        [] if satisfies(manifest.plugin_api) else [f"{manifest.plugin_api} excludes this engine ({PLUGIN_API_VERSION})"],
    )
    if not report.ok:
        return report

    with tempfile.TemporaryDirectory() as tmp:
        state = PluginState(Path(tmp) / "state.json")
        state.set_enabled(manifest.id, True)
        runtime = PluginRuntime(sources=[], state=state)
        loaded = runtime._load_one(plugin_dir, tier, set())
        built: list[Tool] = []
        if loaded.status == "loaded":
            # What an agent loop does: build the tools for its own workspace.
            runtime.plugins = [loaded]
            built = runtime.build_tools(workspace=Path(tmp))
    if loaded.status == "unavailable":
        # Its SDK isn't installed here: nothing to load, nothing to check.
        report.skipped.append(f"load: {loaded.reason}")
        return report
    if loaded.status != "loaded":
        report.check("load", [f"{loaded.status}: {loaded.reason}"])
        return report
    report.check("load", [])
    if manifest.skills:
        report.check("skills", check_skills(manifest))
    if loaded.tools:
        missing = sorted({t.name for t in loaded.tools} - {t.name for t in built})
        report.check("tools build for an agent loop", [f"could not build {name}" for name in missing])
    resolved = runtime.resolve_channels(loaded) if loaded.channel_entries else {}
    for name in loaded.channel_entries:
        if name not in resolved:
            report.check(f"channel {name}", [f"could not be imported: {loaded.reason}"])
    for name, cls in resolved.items():
        report.check(f"channel {name}", check_channel(cls, name))
    for tool in loaded.tools:
        report.check(f"tool {tool.name}", check_tool(tool))
    for i, hook in enumerate(loaded.hooks):
        report.check(f"hook #{i + 1}", check_hook(hook))
    return report


def assert_plugin_contract(plugin_dir: str | Path, tier: Tier = "community") -> ContractReport:
    """pytest helper: fail with every contract problem listed."""
    report = check_plugin(plugin_dir, tier)
    assert report.ok, f"plugin {report.plugin_id} breaks the contract:\n  " + "\n  ".join(report.problems)
    return report
