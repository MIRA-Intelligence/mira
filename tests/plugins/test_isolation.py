"""Experimental plugins run out of process, sandboxed where possible (mira#211)."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from mira_engine.agent.tools.sandbox import sandbox_available, seatbelt_available
from mira_engine.plugins import PluginRuntime, PluginSource
from mira_engine.plugins.state import PluginState

PROBE = '''
import asyncio, os, socket, sys
from mira_engine.agent.tools.base import Tool

class Probe(Tool):
    name = "probe"
    description = "isolation probe"
    parameters = {"type": "object", "properties": {"what": {"type": "string"}, "path": {"type": "string"}}}

    async def execute(self, what="pid", path="", **kw):
        if what == "pid":
            return str(os.getpid())
        if what == "env":
            return os.environ.get("MIRA_TEST_SECRET", "absent")
        if what == "crash":
            os._exit(3)
        if what == "hang":
            await asyncio.sleep(3600)
        if what == "print":
            print("noise on stdout")
            return "still fine"
        if what == "net":
            try:
                socket.create_connection(("1.1.1.1", 53), timeout=3).close()
                return "connected"
            except OSError as e:
                return "blocked: " + type(e).__name__
        if what == "read":
            try:
                return open(path).read()[:50]
            except OSError as e:
                return "blocked: " + type(e).__name__
        if what == "write":
            try:
                with open(path, "w") as f:
                    f.write("x")
                return "written"
            except OSError as e:
                return "blocked: " + type(e).__name__
        if what == "run":
            import subprocess
            try:
                return subprocess.run([sys.executable, "-c", "print('ran')"], capture_output=True, text=True).stdout.strip()
            except OSError as e:
                return "blocked: " + type(e).__name__
        return "?"
'''


def _plugin(root: Path, pid: str, manifest: dict | None = None) -> Path:
    d = root / pid
    d.mkdir(parents=True)
    data = {
        "id": pid, "version": "1.0.0", "default_enabled": True,
        "provides": {"tools": [{"name": "probe", "entry": "probe:Probe"}]}, **(manifest or {}),
    }
    (d / "plugin.json").write_text(json.dumps(data))
    (d / "probe.py").write_text(textwrap.dedent(PROBE))
    return d


def _rt(tmp_path: Path, *, unsandboxed: bool, workspace: Path | None = None) -> PluginRuntime:
    rt = PluginRuntime(
        sources=[PluginSource(tmp_path / "plugins", "experimental")],
        state=PluginState(tmp_path / "state.json"),
        workspace=workspace or tmp_path / "ws",
        allow_unsandboxed=unsandboxed,
    )
    rt.load()
    return rt


def test_without_a_sandbox_experimental_plugins_need_an_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr("mira_engine.agent.tools.sandbox.plugin_sandbox", lambda network=True: (None, "no bwrap"))
    _plugin(tmp_path / "plugins", "exp")
    p = _rt(tmp_path, unsandboxed=False).get("exp")
    assert p.status == "unsupported" and "unsandboxed_experimental" in p.reason


def test_out_of_process_plugins_cannot_provide_channels_or_hooks(tmp_path):
    hooks = {"provides": {"hooks": [{"entry": "probe:Probe"}]}}
    _plugin(tmp_path / "plugins", "hooky", hooks)
    p = _rt(tmp_path, unsandboxed=True).get("hooky")
    assert p.status == "failed" and "only provide tools" in p.reason


@pytest.fixture
def unsandboxed(tmp_path, monkeypatch):
    monkeypatch.setattr("mira_engine.agent.tools.sandbox.plugin_sandbox", lambda network=True: (None, "test"))
    monkeypatch.setenv("MIRA_TEST_SECRET", "sk-should-not-leak")
    _plugin(tmp_path / "plugins", "exp")
    rt = _rt(tmp_path, unsandboxed=True)
    assert rt.get("exp").status == "loaded", rt.get("exp").reason
    return rt.tools()[0]


async def test_tools_run_in_another_process_without_the_engine_env(unsandboxed):
    tool = unsandboxed
    assert int(await tool.execute(what="pid")) != os.getpid()
    assert await tool.execute(what="env") == "absent"
    assert await tool.execute(what="print") == "still fine"  # plugin stdout can't corrupt the protocol


async def test_a_crashing_plugin_never_takes_the_engine_down(unsandboxed):
    tool = unsandboxed
    first = await tool.execute(what="pid")
    assert "exited" in await tool.execute(what="crash")
    assert await tool.execute(what="pid") != first  # a fresh host on the next call


async def test_a_hung_plugin_call_is_killed_and_the_host_replaced(unsandboxed):
    tool = unsandboxed
    tool._inner._call_timeout_s = 0.5
    first = await tool.execute(what="pid")
    assert first.isdigit(), first
    assert "Error" in await tool.execute(what="hang")
    second = await tool.execute(what="pid")
    assert second.isdigit() and second != first


async def test_a_slow_host_start_does_not_count_against_the_call(tmp_path, monkeypatch):
    # Importing the engine and the plugin takes a while on a cold start; the
    # tool's own timeout only covers the call.
    monkeypatch.setattr("mira_engine.agent.tools.sandbox.plugin_sandbox", lambda network=True: (None, "test"))
    d = _plugin(tmp_path / "plugins", "slowstart")
    (d / "probe.py").write_text("import time\ntime.sleep(1.0)\n" + (d / "probe.py").read_text())
    rt = _rt(tmp_path, unsandboxed=True)
    assert rt.get("slowstart").status == "loaded", rt.get("slowstart").reason
    tool = rt.tools()[0]
    tool._inner._call_timeout_s = 0.5
    assert (await tool.execute(what="pid")).isdigit()


async def test_killing_a_hung_host_fails_the_other_calls_waiting_on_it(unsandboxed):
    import asyncio

    client = unsandboxed._inner._client
    hung = asyncio.create_task(client.call("probe", {"what": "hang"}, 0.5))
    other = asyncio.create_task(client.call("probe", {"what": "hang"}, 30))
    with pytest.raises(asyncio.TimeoutError):
        await hung
    # The host is gone; the other call must not wait out its own 30 s.
    with pytest.raises(RuntimeError, match="exited"):
        await asyncio.wait_for(other, 5)


async def test_the_tripwire_backs_up_a_missing_sandbox(tmp_path, monkeypatch):
    # Windows, or Linux without bwrap: the host runs unsandboxed and only the
    # in-host tripwire checks the manifest's permissions.
    monkeypatch.setattr("mira_engine.agent.tools.sandbox.plugin_sandbox", lambda network=True: (None, "test"))
    mira_home = Path(os.environ["MIRA_HOME"])
    mira_home.mkdir(parents=True, exist_ok=True)
    (mira_home / "config.json").write_text('{"providers": {"key": "sk-secret"}}')
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.txt").write_text("workspace data")
    _plugin(tmp_path / "plugins", "noperm")
    _plugin(tmp_path / "plugins", "writer", {"permissions": {"filesystem": "workspace:write", "shell": True}})
    rt = _rt(tmp_path, unsandboxed=True, workspace=ws)
    noperm = next(t for t in rt.tools() if t.plugin_id == "noperm")
    writer = next(t for t in rt.tools() if t.plugin_id == "writer")
    assert await noperm.execute(what="net") == "blocked: PermissionError"
    assert await noperm.execute(what="read", path=str(mira_home / "config.json")) == "blocked: PermissionError"
    assert await noperm.execute(what="write", path=str(mira_home / "leak.txt")) == "blocked: PermissionError"
    assert await noperm.execute(what="run") == "blocked: PermissionError"
    assert not (mira_home / "leak.txt").exists()
    assert await writer.execute(what="read", path=str(ws / "data.txt")) == "workspace data"
    assert await writer.execute(what="write", path=str(ws / "new.txt")) == "written"
    assert await writer.execute(what="run") == "ran"


def test_registry_tier_promotes_a_plugin_in_process(tmp_path):
    _plugin(tmp_path / "plugins", "trusted")
    state = PluginState(tmp_path / "state.json")
    state.set_registry_tiers({"trusted": "verified"})
    rt = PluginRuntime(sources=[PluginSource(tmp_path / "plugins", "experimental")], state=state, workspace=tmp_path, allow_unsandboxed=False)
    rt.load()
    p = rt.get("trusted")
    assert p.tier == "verified" and p.status == "loaded" and p.reason == ""


BWRAP_OK = sandbox_available(network=False)[0]


@pytest.mark.skipif(not BWRAP_OK, reason="needs bubblewrap (Linux)")
async def test_sandbox_enforces_network_and_filesystem_permissions(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.txt").write_text("workspace data")
    secret = Path(os.environ.get("MIRA_HOME", tmp_path / "mira-home")) / "config.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"providers": {"key": "sk-secret"}}')
    _plugin(tmp_path / "plugins", "noperm")
    _plugin(tmp_path / "plugins", "reader", {"permissions": {"filesystem": "workspace:read"}})
    rt = _rt(tmp_path, unsandboxed=False, workspace=ws)
    noperm = next(t for t in rt.tools() if t.plugin_id == "noperm")
    reader = next(t for t in rt.tools() if t.plugin_id == "reader")
    assert (await noperm.execute(what="net")).startswith("blocked")
    assert (await noperm.execute(what="read", path=str(ws / "data.txt"))).startswith("blocked")
    assert await reader.execute(what="read", path=str(ws / "data.txt")) == "workspace data"
    assert (await reader.execute(what="read", path=str(secret))).startswith("blocked")


@pytest.mark.skipif(not BWRAP_OK, reason="needs bubblewrap (Linux)")
async def test_sandbox_sees_plugins_and_workspace_under_the_mira_home(tmp_path):
    # The real layout: ~/.mira/plugins/<id> and the default ~/.mira/workspace.
    home = Path(os.environ["MIRA_HOME"])
    ws = home / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "data.txt").write_text("workspace data")
    (home / "config.json").write_text('{"providers": {"key": "sk-secret"}}')
    _plugin(home / "plugins", "reader", {"permissions": {"filesystem": "workspace:read"}})
    rt = PluginRuntime(
        sources=[PluginSource(home / "plugins", "experimental")],
        state=PluginState(tmp_path / "state.json"),
        workspace=ws,
        allow_unsandboxed=False,
    )
    rt.load()
    assert rt.get("reader").status == "loaded", rt.get("reader").reason
    reader = next(t for t in rt.tools() if t.plugin_id == "reader")
    assert await reader.execute(what="read", path=str(ws / "data.txt")) == "workspace data"
    assert (await reader.execute(what="read", path=str(home / "config.json"))).startswith("blocked")


SEATBELT_OK = sys.platform == "darwin" and seatbelt_available()[0]


@pytest.mark.skipif(not SEATBELT_OK, reason="needs macOS sandbox-exec")
async def test_seatbelt_enforces_network_files_and_shell(tmp_path, monkeypatch):
    # The real layout: plugins in <mira home>/plugins; a home directory with a
    # private file; a workspace outside the home.
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    (home_dir / "private.txt").write_text("private")
    monkeypatch.setenv("HOME", str(home_dir))
    # The engine's working directory is one the sandbox hides: the host must
    # not depend on it (it runs in its scratch dir).
    monkeypatch.chdir(home_dir)
    mira_home = Path(os.environ["MIRA_HOME"])
    mira_home.mkdir(parents=True, exist_ok=True)
    (mira_home / "config.json").write_text('{"providers": {"key": "sk-secret"}}')
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.txt").write_text("workspace data")
    _plugin(mira_home / "plugins", "noperm")
    _plugin(mira_home / "plugins", "writer", {"permissions": {"filesystem": "workspace:write", "shell": True}})
    rt = PluginRuntime(
        sources=[PluginSource(mira_home / "plugins", "experimental")],
        state=PluginState(tmp_path / "state.json"),
        workspace=ws,
        allow_unsandboxed=False,
    )
    rt.load()
    for pid in ("noperm", "writer"):
        assert rt.get(pid).status == "loaded" and rt.get(pid).reason == "out of process (seatbelt)", rt.get(pid).reason
    noperm = next(t for t in rt.tools() if t.plugin_id == "noperm")
    writer = next(t for t in rt.tools() if t.plugin_id == "writer")

    assert (await noperm.execute(what="net")).startswith("blocked")
    assert (await noperm.execute(what="read", path=str(ws / "data.txt"))).startswith("blocked")
    assert (await noperm.execute(what="read", path=str(mira_home / "config.json"))).startswith("blocked")
    assert (await noperm.execute(what="read", path=str(home_dir / "private.txt"))).startswith("blocked")
    assert (await noperm.execute(what="write", path=str(tmp_path / "out.txt"))).startswith("blocked")
    assert (await noperm.execute(what="run")).startswith("blocked")
    assert not (tmp_path / "out.txt").exists()

    assert await writer.execute(what="read", path=str(ws / "data.txt")) == "workspace data"
    assert await writer.execute(what="write", path=str(ws / "new.txt")) == "written"
    assert (await writer.execute(what="read", path=str(mira_home / "config.json"))).startswith("blocked")
    assert await writer.execute(what="run") == "ran"
