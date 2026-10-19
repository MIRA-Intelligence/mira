"""Remote disable list from the plugin registry (mira#210)."""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.plugins import PluginRuntime, PluginSource
from mira_engine.plugins.remote import RemoteDisablePoller, poller_from_config
from mira_engine.plugins.state import PluginState

TOOL = """
from mira_engine.agent.tools.base import Tool

class Echo(Tool):
    name = "echo_plugin"
    description = "echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text="", **kw):
        return "echo:" + text
"""
TOOL_MANIFEST = {"provides": {"tools": [{"name": "echo_plugin", "entry": "tool:Echo"}]}}


def _plugin(root, pid, manifest, files):
    import json

    d = root / pid
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"id": pid, "version": "1.0.0", "default_enabled": True, **manifest}))
    for name, body in files.items():
        (d / name).write_text(body)
    return d


def _rt(tmp_path):
    rt = PluginRuntime(sources=[PluginSource(tmp_path / "plugins", "community")], state=PluginState(tmp_path / "s.json"))
    rt.load()
    return rt


class _Registry:
    def __init__(self, payload=None, fail=False):
        self.payload = payload or {"disabled": []}
        self.fail = fail
        self.urls: list[str] = []

    async def __call__(self, url):
        self.urls.append(url)
        if self.fail:
            raise OSError("registry down")
        return self.payload


async def test_a_listed_version_stops_now_and_on_the_next_start(tmp_path):
    _plugin(tmp_path / "plugins", "flaky", TOOL_MANIFEST, {"tool.py": TOOL})
    rt = _rt(tmp_path)
    tool = rt.tools()[0]
    reg = _Registry({"disabled": [{"id": "flaky", "versions": ["1.0.0"], "reasons": {"1.0.0": "leaks tokens"}}]})
    poller = RemoteDisablePoller(rt, "https://reg.example/community/", fetch=reg)
    assert await poller.poll_once() == ["flaky"]
    assert reg.urls == ["https://reg.example/community/plugins/disabled"]
    assert "unavailable" in await tool.execute(text="hi")
    p = _rt(tmp_path).get("flaky")
    assert p.status == "remote-disabled" and "leaks tokens" in p.reason
    # Polling the same list again changes nothing.
    assert await poller.poll_once() == []

    # The registry lifts it: it works again, without the user doing anything.
    reg.payload = {"disabled": []}
    await poller.poll_once()
    assert await tool.execute(text="hi") == "echo:hi"
    assert _rt(tmp_path).get("flaky").status == "loaded"


async def test_other_versions_and_star(tmp_path):
    _plugin(tmp_path / "plugins", "flaky", TOOL_MANIFEST, {"tool.py": TOOL})
    rt = _rt(tmp_path)
    poller = RemoteDisablePoller(rt, "https://reg", fetch=_Registry({"disabled": [{"id": "flaky", "versions": ["0.9.0"]}]}))
    assert await poller.poll_once() == []
    assert await rt.tools()[0].execute(text="x") == "echo:x"
    poller._fetch = _Registry({"disabled": [{"id": "flaky", "versions": ["*"], "reasons": {"*": "all"}}]})
    assert await poller.poll_once() == ["flaky"]


async def test_an_unreachable_registry_keeps_the_last_list(tmp_path):
    _plugin(tmp_path / "plugins", "flaky", TOOL_MANIFEST, {"tool.py": TOOL})
    rt = _rt(tmp_path)
    poller = RemoteDisablePoller(rt, "https://reg", fetch=_Registry({"disabled": [{"id": "flaky", "versions": ["*"]}]}))
    await poller.poll_once()
    poller._fetch = _Registry(fail=True)
    assert await poller.poll_once() == []
    assert "unavailable" in await rt.tools()[0].execute(text="x")


def test_the_poller_follows_config():
    cfg = SimpleNamespace(plugins=SimpleNamespace(remote_disable=False, registry_url="https://r", poll_interval_s=600))
    assert poller_from_config(cfg) is None
    cfg.plugins.remote_disable = True
    poller = poller_from_config(cfg)
    assert poller is not None and poller.url == "https://r/plugins/disabled" and poller.interval_s == 600
