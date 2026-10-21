"""In-process permission tripwire (plugins.permissions)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import textwrap
from pathlib import Path

import pytest

from mira_engine.plugins import PluginRuntime, PluginSource
from mira_engine.plugins.state import PluginState

PROBE = '''
import os, socket, subprocess, sys
from mira_engine.agent.tools.base import Tool

class Probe(Tool):
    name = "perm_probe"
    description = "permission probe"
    parameters = {"type": "object", "properties": {"what": {"type": "string"}, "arg": {"type": "string"}}}

    def __init__(self, ctx):
        self.ctx = ctx

    async def execute(self, what="", arg="", **kw):
        if what == "connect":
            host, port = arg.split(":")
            with socket.create_connection((host, int(port)), timeout=2):
                return "connected"
        if what == "write":
            with open(arg, "w") as f:
                f.write("x")
            return "written"
        if what == "data":
            p = self.ctx.data_dir / "state.txt"
            p.write_text("x")
            return p.read_text()
        if what == "read":
            with open(arg) as f:
                return f.read()
        if what == "run":
            return subprocess.run([sys.executable, "-c", "print('ran')"], capture_output=True, text=True).stdout.strip()
        if what == "rm":
            os.remove(arg)
            return "removed"
        return "?"
'''


def _plugin(root: Path, pid: str, perms: dict | None = None) -> None:
    d = root / pid
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({
        "id": pid, "version": "1.0.0", "default_enabled": True,
        "provides": {"tools": [{"name": "perm_probe", "entry": "probe:Probe"}]},
        "permissions": perms or {},
    }))
    (d / "probe.py").write_text(textwrap.dedent(PROBE))


def _tool(tmp_path: Path, perms: dict | None = None, tier: str = "community", workspace: Path | None = None, **rt_kw):
    pid = f"p{abs(hash(json.dumps(perms, sort_keys=True))) % 10**8}"
    _plugin(tmp_path / "plugins", pid, perms)
    rt = PluginRuntime(sources=[PluginSource(tmp_path / "plugins", tier)], state=PluginState(tmp_path / "state.json"), **rt_kw)
    rt._config = None
    rt.load()
    assert rt.get(pid).status == "loaded", rt.get(pid).reason
    tool = next(t for t in rt.build_tools(workspace=workspace or tmp_path / "ws") if t.plugin_id == pid)
    tool._breaker.threshold = 100  # these tests look at single calls, not at tripping
    return tool


@pytest.fixture(autouse=True)
def no_temp_allowance(monkeypatch):
    # pytest's tmp_path is inside the system temp dir, which plugins may
    # write to; take that allowance away so tmp_path counts as "elsewhere".
    monkeypatch.setattr("mira_engine.plugins.permissions._temp_roots", lambda: [])


@pytest.fixture
def server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    yield f"127.0.0.1:{srv.getsockname()[1]}"
    srv.close()


@pytest.fixture
def mira_home() -> Path:
    home = Path(os.environ["MIRA_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text('{"providers": {"openai": {"apiKey": "sk-secret"}}}')
    return home


async def test_network_needs_a_declaration(tmp_path, server):
    denied = await _tool(tmp_path).execute(what="connect", arg=server)
    assert "may not connect to 127.0.0.1" in denied
    allowed = await _tool(tmp_path, {"network": ["localhost"]}).execute(what="connect", arg=server)
    assert allowed == "connected"


async def test_files_outside_the_grant_are_refused(tmp_path, mira_home):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("workspace data")
    outside = tmp_path / "elsewhere.txt"

    plain = _tool(tmp_path, workspace=ws)
    assert "may not write" in await plain.execute(what="write", arg=str(outside))
    assert "may not write" in await plain.execute(what="write", arg=str(ws / "new.txt"))
    assert "may not read" in await plain.execute(what="read", arg=str(mira_home / "config.json"))
    assert await plain.execute(what="data") == "x"  # its own data dir is fine
    assert not outside.exists()

    writer = _tool(tmp_path, {"filesystem": "workspace:write"}, workspace=ws)
    assert await writer.execute(what="write", arg=str(ws / "new.txt")) == "written"
    assert await writer.execute(what="read", arg=str(ws / "notes.txt")) == "workspace data"
    assert "may not write" in await writer.execute(what="rm", arg=str(outside.parent / "state.json"))


async def test_the_default_workspace_inside_the_mira_home_is_readable_when_granted(tmp_path, mira_home):
    ws = mira_home / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "notes.txt").write_text("in home")
    reader = _tool(tmp_path, {"filesystem": "workspace:read"}, workspace=ws)
    assert await reader.execute(what="read", arg=str(ws / "notes.txt")) == "in home"
    assert "may not read" in await reader.execute(what="read", arg=str(mira_home / "config.json"))


async def test_processes_need_shell(tmp_path):
    assert "may not run" in await _tool(tmp_path).execute(what="run")
    assert await _tool(tmp_path, {"shell": True}).execute(what="run") == "ran"


async def test_first_party_plugins_are_unchecked_unless_audited(tmp_path, mira_home, server, monkeypatch):
    core = _tool(tmp_path, tier="core-bundled")
    assert await core.execute(what="connect", arg=server) == "connected"

    seen: list[str] = []

    class _Log:
        def warning(self, fmt, *args):
            seen.append(fmt.format(*args))

        info = warning

    # Captured at the module (CLI tests may disable mira_engine's loguru output).
    monkeypatch.setattr("mira_engine.plugins.permissions.logger", _Log())
    audited = _tool(tmp_path / "audited", tier="core-bundled", audit_bundled=True)
    assert await audited.execute(what="connect", arg=server) == "connected"  # logged, not blocked
    assert any("plugin permission audit" in s and "connect to 127.0.0.1" in s for s in seen)


async def test_the_grant_never_outlives_the_call(tmp_path, mira_home):
    tool = _tool(tmp_path)
    assert "may not read" in await tool.execute(what="read", arg=str(mira_home / "config.json"))
    # The engine itself, after the call, still reads its own config.
    assert "sk-secret" in (mira_home / "config.json").read_text()


async def test_a_plugin_channel_and_the_tasks_it_starts_run_under_its_grant(tmp_path, monkeypatch):
    from mira_engine.bus.queue import MessageBus
    from mira_engine.channels.manager import ChannelManager
    from mira_engine.config.schema import Config
    from mira_engine.plugins import set_runtime_for_tests

    outside = tmp_path / "leak.txt"
    d = tmp_path / "plugins" / "chatty"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({
        "id": "chatty", "version": "1.0.0", "default_enabled": True,
        "provides": {"channels": [{"name": "chatty", "entry": "ch:Chatty"}]},
    }))
    (d / "ch.py").write_text(textwrap.dedent(f'''
        import asyncio
        from mira_engine.channels.base import BaseChannel

        RESULTS = []

        class Chatty(BaseChannel):
            name = "chatty"

            async def start(self):
                async def background():
                    try:
                        open({str(outside)!r}, "w").write("x")
                        RESULTS.append("wrote")
                    except PermissionError as e:
                        RESULTS.append(str(e))
                await asyncio.create_task(background())

            async def stop(self): ...
            async def send(self, msg): ...
    '''))
    rt = PluginRuntime(sources=[PluginSource(tmp_path / "plugins", "community")], state=PluginState(tmp_path / "state.json"))
    rt._config = None
    rt.load()
    set_runtime_for_tests(rt)
    config = Config()
    config.channels.chatty = {"enabled": True, "allowFrom": ["*"]}
    manager = ChannelManager(config, MessageBus())
    channel = manager.channels["chatty"]
    await manager._start_channel("chatty", channel)
    results = __import__("sys").modules[type(channel).__module__].RESULTS
    assert results and "may not write" in results[0]
    assert not outside.exists()


def test_permission_checks_ignore_unwatched_events_quickly():
    from mira_engine.plugins import permissions

    permissions.install()
    # No grant: the hook returns at once, whatever the event.
    for _ in range(1000):
        permissions._hook("open", ("/etc/hosts", "r", 0))
    assert permissions.current() is None


async def test_concurrent_calls_keep_their_own_grants(tmp_path, server):
    allowed = _tool(tmp_path, {"network": ["localhost"]})
    denied = _tool(tmp_path / "other", {})
    a, b = await asyncio.gather(allowed.execute(what="connect", arg=server), denied.execute(what="connect", arg=server))
    assert a == "connected" and "may not connect" in b


async def test_a_plugin_that_keeps_overstepping_is_switched_off(tmp_path, server):
    tool = _tool(tmp_path)
    tool._breaker.threshold = 3
    for _ in range(3):
        assert "may not connect" in await tool.execute(what="connect", arg=server)
    assert "disabled after repeated failures" in await tool.execute(what="data")
