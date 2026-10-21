"""Plugin context: per-loop tools, config views by permission, paused breakers."""

from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path

import pytest

from mira_engine.plugins import PluginRuntime, PluginSource
from mira_engine.plugins.context import ConfigView, PluginContext
from mira_engine.plugins.state import PluginState

CTX_TOOLS = '''
import json
from mira_engine.agent.tools.base import Tool

class _Base(Tool):
    description = "ctx probe"
    parameters = {"type": "object", "properties": {}}

class PlainTool(_Base):
    name = "plain_tool"

    async def execute(self, **kw):
        return "plain"

class CtxTool(_Base):
    name = "ctx_tool"

    def __init__(self, ctx):
        self.ctx = ctx

    async def execute(self, **kw):
        c = self.ctx
        return json.dumps({
            "workspace": str(c.workspace), "proxy": c.proxy, "host": dict(c.host),
            "token": c.config.get("channels.demo.token"), "other": c.config.get("providers.openai.api_key"),
            "data_dir": str(c.data_dir),
        })

class _Made(_Base):
    name = "made_tool"

    def __init__(self, workspace):
        self.workspace = workspace

    async def execute(self, **kw):
        return str(self.workspace)

def make(ctx):
    if ctx.workspace is not None and ctx.workspace.name == "broken":
        raise RuntimeError("no such workspace")
    return _Made(ctx.workspace)
'''

MANIFEST = {
    "provides": {"tools": [
        {"name": "plain_tool", "entry": "tools:PlainTool"},
        {"name": "ctx_tool", "entry": "tools:CtxTool"},
        {"name": "made_tool", "entry": "tools:make"},
    ]},
    "permissions": {"secrets": ["channels.demo.token"]},
}


def _plugin(root: Path, pid: str = "ctxdemo", manifest: dict | None = None) -> Path:
    d = root / pid
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"id": pid, "version": "1.0.0", "default_enabled": True, **(manifest or MANIFEST)}))
    (d / "tools.py").write_text(textwrap.dedent(CTX_TOOLS))
    return d


class _Cfg:
    def model_dump(self):
        return {"channels": {"demo": {"token": "tok-demo"}}, "providers": {"openai": {"api_key": "sk-openai"}}}


def _rt(tmp_path: Path, tier: str) -> PluginRuntime:
    rt = PluginRuntime(sources=[PluginSource(tmp_path / "plugins", tier)], state=PluginState(tmp_path / "state.json"))
    rt._config = _Cfg()
    rt.load()
    return rt


def _by_name(tools):
    return {t.name: t for t in tools}


def test_config_view_limits_paths_and_normalises_case():
    data = {"channels": {"discord": {"token": "t", "allowFrom": ["x"]}}, "providers": {"openai": {"apiKey": "k"}}}
    view = ConfigView(lambda: data, ("channels.discord",))
    assert view.get("channels.discord.token") == "t"
    assert view.get("channels.discord.allow_from") == ["x"]
    assert view.get("providers.openai.api_key") is None
    assert view.get("providers.openai.api_key", "-") == "-"
    assert ConfigView(lambda: data).get("providers.openai.api_key") == "k"
    flat = ConfigView.from_values({"channels.discord.token": "t"})
    assert flat.get("channels.discord.token") == "t" and flat.get("channels") is None


def test_config_view_is_lazy():
    calls = []
    view = ConfigView(lambda: calls.append(1) or {}, ())
    assert view.get("anything") is None and calls == []


async def test_every_loop_builds_its_own_tools_with_its_context(tmp_path):
    _plugin(tmp_path / "plugins")
    rt = _rt(tmp_path, "core-bundled")
    assert rt.get("ctxdemo").status == "loaded", rt.get("ctxdemo").reason
    a = _by_name(rt.build_tools(workspace=tmp_path / "ws-a", proxy="http://proxy:1", host={"brave_api_key": "bk"}))
    b = _by_name(rt.build_tools(workspace=tmp_path / "ws-b"))
    assert set(a) == {"plain_tool", "ctx_tool", "made_tool"}
    assert a["ctx_tool"]._inner is not b["ctx_tool"]._inner
    assert a["ctx_tool"]._breaker is b["ctx_tool"]._breaker is rt.breaker("ctxdemo")
    assert await a["made_tool"].execute() == str(tmp_path / "ws-a")
    assert await b["made_tool"].execute() == str(tmp_path / "ws-b")
    seen = json.loads(await a["ctx_tool"].execute())
    assert seen["workspace"] == str(tmp_path / "ws-a") and seen["proxy"] == "http://proxy:1"
    # Core-bundled: the engine's host values and the whole config.
    assert seen["host"] == {"brave_api_key": "bk"} and seen["other"] == "sk-openai"
    assert Path(seen["data_dir"]).is_dir() and Path(seen["data_dir"]).name == "ctxdemo"


async def test_other_tiers_only_see_their_declared_secrets(tmp_path):
    _plugin(tmp_path / "plugins")
    rt = _rt(tmp_path, "community")
    seen = json.loads(await _by_name(rt.build_tools(host={"brave_api_key": "bk"}))["ctx_tool"].execute())
    assert seen["token"] == "tok-demo"
    assert seen["other"] is None and seen["host"] == {}


async def test_a_tool_that_cannot_be_built_for_a_loop_is_left_out(tmp_path):
    _plugin(tmp_path / "plugins")
    rt = _rt(tmp_path, "core-bundled")
    tools = _by_name(rt.build_tools(workspace=tmp_path / "broken"))
    assert set(tools) == {"plain_tool", "ctx_tool"}


def test_a_factory_that_fails_at_load_marks_the_plugin_failed(tmp_path):
    manifest = {"provides": {"tools": [{"name": "made_tool", "entry": "tools:missing"}]}}
    _plugin(tmp_path / "plugins", manifest=manifest)
    p = _rt(tmp_path, "core-bundled").get("ctxdemo")
    assert p.status == "failed" and "missing" in p.reason


FLAKY = '''
from mira_engine.agent.tools.base import Tool

class Flaky(Tool):
    name = "flaky_tool"
    description = "fails"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kw):
        raise RuntimeError("down")
'''


async def test_first_party_plugins_pause_instead_of_tripping_for_good(tmp_path, monkeypatch):
    d = tmp_path / "plugins" / "flaky"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({
        "id": "flaky", "version": "1.0.0", "provides": {"tools": [{"name": "flaky_tool", "entry": "tool:Flaky"}]},
    }))
    (d / "tool.py").write_text(textwrap.dedent(FLAKY))
    rt = _rt(tmp_path, "core-bundled")
    tool = rt.build_tools()[0]
    for _ in range(3):
        assert "failed: down" in await tool.execute()
    assert "paused" in await tool.execute()
    assert rt.state.tripped("flaky") is None  # nothing persisted
    assert _rt(tmp_path, "core-bundled").get("flaky").status == "loaded"
    now = time.monotonic()
    monkeypatch.setattr("mira_engine.plugins.guard.time.monotonic", lambda: now + 301)
    assert "failed: down" in await tool.execute()  # back after the cool-down


@pytest.fixture
def unsandboxed(monkeypatch):
    monkeypatch.setattr("mira_engine.agent.tools.sandbox.sandbox_available", lambda network=True: (False, "test"))


async def test_out_of_process_tools_get_their_context_from_the_engine(tmp_path, unsandboxed):
    manifest = {**MANIFEST, "permissions": {"secrets": ["channels.demo.token"], "filesystem": "workspace:read"}}
    _plugin(tmp_path / "plugins", manifest=manifest)
    rt = PluginRuntime(
        sources=[PluginSource(tmp_path / "plugins", "experimental")],
        state=PluginState(tmp_path / "state.json"),
        workspace=tmp_path / "ws",
        allow_unsandboxed=True,
    )
    rt._config = _Cfg()
    rt.load()
    assert rt.get("ctxdemo").status == "loaded", rt.get("ctxdemo").reason
    tools = _by_name(rt.build_tools(workspace=tmp_path / "other", host={"brave_api_key": "bk"}))
    assert set(tools) == {"plain_tool", "ctx_tool", "made_tool"}
    seen = json.loads(await tools["ctx_tool"].execute())
    assert seen["token"] == "tok-demo" and seen["other"] is None and seen["host"] == {}
    # The host's workspace is the engine's, granted by the manifest (one host per plugin).
    assert seen["workspace"] == str(tmp_path / "ws")


def test_plugin_context_defaults():
    ctx = PluginContext(plugin_id="x", tier="community")
    assert ctx.workspace is None and dict(ctx.host) == {} and ctx.config.get("a.b") is None
