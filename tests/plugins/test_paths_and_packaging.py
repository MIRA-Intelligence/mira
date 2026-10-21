"""Plugin paths, lazy kernel imports and packaging hooks."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from mira_engine.plugins import PluginRuntime, PluginSource, paths
from mira_engine.plugins.state import PluginState

REPO = Path(__file__).resolve().parents[2]


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO)}


def test_paths_module_does_not_import_the_loader():
    # Code that runs while the config loads (provider lookups, the skill pack
    # manager) uses mira_engine.plugins.paths; it must not import the loader,
    # whose agent imports would cycle back into a half-loaded config module.
    code = (
        "import sys, mira_engine.plugins.paths as p; p.bundled_plugins_dir(); "
        "print(','.join(m for m in ('mira_engine.plugins.loader', 'mira_engine.plugins.guard') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=_env(), capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_host_command_uses_the_frozen_executable(monkeypatch):
    assert paths.host_command() == [sys.executable, "-m", "mira_engine.plugins.host"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.host_command() == [sys.executable, "--mira-plugin-host"]


def test_host_env_puts_a_source_checkout_but_not_site_packages_on_the_path(monkeypatch):
    import mira_engine
    from mira_engine.plugins import isolation

    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    env = isolation.host_env()
    assert env["PYTHONPATH"].split(os.pathsep) == [str(REPO), "/elsewhere"]
    assert "OPENAI_API_KEY" not in env and env["PYTHONDONTWRITEBYTECODE"] == "1"
    # An installed engine: its site dir must not outrank the standard library.
    monkeypatch.setattr(mira_engine, "__file__", "/venv/lib/python3.12/site-packages/mira_engine/__init__.py")
    assert isolation.host_env()["PYTHONPATH"] == "/elsewhere"


def test_plugin_env_carries_only_what_the_manifest_grants(tmp_path, monkeypatch):
    from mira_engine.config.paths import get_data_dir
    from mira_engine.plugins import isolation
    from mira_engine.plugins.manifest import parse_manifest

    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    tools = {"tools": [{"name": "t", "entry": "t:T"}]}
    manifest = parse_manifest({
        "id": "p", "version": "1.0.0", "provides": tools,
        "permissions": {"filesystem": "workspace:read", "secrets": ["channels.demo.token"]},
    }, tmp_path)
    env = isolation.plugin_env(manifest, workspace=tmp_path / "ws", config={"channels.demo.token": "tok"}, tier="community")
    scratch = get_data_dir() / "plugin-data" / "p"
    assert env["MIRA_PLUGIN_DATA_DIR"] == env["TMPDIR"] == str(scratch) and scratch.is_dir()
    assert env["MIRA_PLUGIN_MIRA_HOME"] == str(get_data_dir()) and env["MIRA_PLUGIN_TIER"] == "community"
    assert env["MIRA_PLUGIN_WORKSPACE"] == str(tmp_path / "ws")
    assert json.loads(env["MIRA_PLUGIN_CONFIG"]) == {"channels.demo.token": "tok"}
    assert "OPENAI_API_KEY" not in env
    closed = parse_manifest({"id": "q", "version": "1.0.0", "provides": tools}, tmp_path)
    env = isolation.plugin_env(closed, workspace=tmp_path / "ws", config={}, tier=None)
    assert "MIRA_PLUGIN_WORKSPACE" not in env and "MIRA_PLUGIN_CONFIG" not in env
    assert env["MIRA_PLUGIN_TIER"] == "experimental"


def test_first_party_plugins_are_audited_only_when_asked(monkeypatch):
    monkeypatch.delenv("MIRA_PLUGIN_AUDIT", raising=False)
    assert PluginRuntime(sources=[]).audit_bundled is False
    monkeypatch.setenv("MIRA_PLUGIN_AUDIT", "1")
    assert PluginRuntime(sources=[]).audit_bundled is True


def test_frozen_entry_dispatches_the_plugin_host(tmp_path):
    plugin = tmp_path / "probe"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(json.dumps({
        "id": "probe", "version": "1.0.0", "provides": {"tools": [{"name": "probe", "entry": "tool:Probe"}]},
    }))
    (plugin / "tool.py").write_text(textwrap.dedent("""
        from mira_engine.agent.tools.base import Tool

        class Probe(Tool):
            name = "probe"
            description = "probe"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kw):
                return "ok"
    """))
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mira_engine_entry.py"), "--mira-plugin-host", "--describe", str(plugin)],
        cwd=REPO, env=_env(), capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1])["tools"][0]["name"] == "probe"


def _echo_plugin(root: Path, reply: str) -> None:
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps({
        "id": "samename", "version": "1.0.0", "default_enabled": True,
        "provides": {"tools": [{"name": "same_tool", "entry": "tool:Same"}]},
    }))
    (root / "tool.py").write_text(textwrap.dedent(f"""
        from mira_engine.agent.tools.base import Tool

        class Same(Tool):
            name = "same_tool"
            description = "same"
            parameters = {{"type": "object", "properties": {{}}}}

            async def execute(self, **kw):
                return "{reply}"
    """))


def test_same_plugin_id_from_another_directory_is_not_served_stale(tmp_path):
    _echo_plugin(tmp_path / "a" / "samename", "from a")
    _echo_plugin(tmp_path / "b" / "samename", "from b")
    results = []
    for src in ("a", "b"):
        rt = PluginRuntime(sources=[PluginSource(tmp_path / src, "community")], state=PluginState(tmp_path / f"{src}.json"))
        rt.load()
        results.append(asyncio.run(rt.tools()[0].execute()))
    assert results == ["from a", "from b"]


def test_desktop_build_ships_bundled_plugins_and_only_builtin_skills():
    spec = (REPO / "mira-engine.spec").read_text()
    assert "_bundled_plugin_datas()" in spec and "mira_engine/_bundled_plugins" in spec
    assert "'mira_engine.plugins.host'" in spec
    assert "'skills/builtin_skills/**/*'" in spec and "'skills/**/*'" not in spec


def test_loading_plugins_does_not_import_channel_sdks(tmp_path):
    # Every agent loop (CLI, SDK) loads the plugin runtime; channel SDKs are
    # only imported when the gateway asks for the channel classes.
    code = (
        "import sys; from mira_engine.plugins import get_runtime; rt = get_runtime(); "
        "assert rt.get('telegram').status == 'loaded', rt.get('telegram').reason; "
        "print(','.join(m for m in ('lark_oapi', 'telegram', 'slack_sdk', 'nio') if m in sys.modules))"
    )
    env = {**_env(), "MIRA_HOME": str(tmp_path / "home")}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""
