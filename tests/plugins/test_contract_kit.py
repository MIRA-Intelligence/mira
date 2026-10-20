"""Contract kit, `mira plugin` CLI and the bundled plugins (mira#208, #209)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mira_engine.cli.commands import app
from mira_engine.plugins.loader import bundled_plugins_dir
from mira_engine.plugins.testing import assert_plugin_contract, check_plugin

runner = CliRunner()
BUNDLED = bundled_plugins_dir()
BUNDLED_PLUGINS = sorted(p for p in BUNDLED.iterdir() if (p / "plugin.json").is_file()) if BUNDLED else []


@pytest.mark.parametrize("plugin_dir", BUNDLED_PLUGINS, ids=lambda p: p.name)
def test_every_bundled_plugin_passes_the_contract(plugin_dir):
    # Runs in mira/verify on the landing branch: a plugin-lane PR that breaks
    # the contract cannot turn green.
    assert_plugin_contract(plugin_dir, tier="core-bundled")


def test_discord_is_a_bundled_plugin_channel():
    assert any(p.name == "discord" for p in BUNDLED_PLUGINS)
    from mira_engine.channels.registry import discover_all
    from mira_engine.plugins import get_runtime

    assert get_runtime().get("discord").status == "loaded"
    assert discover_all()["discord"].__module__ == "mira_plugins.discord.discord_channel"


def _write(d: Path, manifest: dict, files: dict[str, str]) -> Path:
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"id": d.name, "version": "1.0.0", **manifest}))
    for name, body in files.items():
        (d / name).write_text(body)
    return d


def test_contract_flags_a_bad_tool(tmp_path):
    bad = (
        "from mira_engine.agent.tools.base import Tool\n"
        "class T(Tool):\n"
        "    name = 'bad name!'\n"
        "    description = ''\n"
        "    parameters = {'type': 'array'}\n"
        "    def execute(self, **kw):\n"
        "        return 'sync'\n"
    )
    d = _write(tmp_path / "bad-tool", {"provides": {"tools": [{"name": "bad name!", "entry": "t:T"}]}}, {"t.py": bad})
    report = check_plugin(d)
    assert not report.ok
    text = "\n".join(report.problems)
    assert "name" in text and "description" in text and "parameters" in text and "async" in text


def test_contract_flags_a_channel_that_explodes_on_construction(tmp_path):
    ch = (
        "from mira_engine.channels.base import BaseChannel\n"
        "class C(BaseChannel):\n"
        "    name = 'boomchat'\n"
        "    def __init__(self, *a, **kw):\n"
        "        raise RuntimeError('connects in __init__')\n"
        "    async def start(self): ...\n"
        "    async def stop(self): ...\n"
        "    async def send(self, msg): ...\n"
    )
    d = _write(tmp_path / "boom", {"provides": {"channels": [{"name": "boomchat", "entry": "c:C"}]}}, {"c.py": ch})
    assert any("constructing" in p for p in check_plugin(d).problems)


def test_contract_flags_a_failing_hook(tmp_path):
    hook = (
        "from mira_engine.agent.hook import AgentHook\n"
        "class H(AgentHook):\n"
        "    async def before_iteration(self, context):\n"
        "        raise ValueError('nope')\n"
    )
    d = _write(tmp_path / "bad-hook", {"provides": {"hooks": [{"entry": "h:H"}]}}, {"h.py": hook})
    assert any("raised ValueError" in p for p in check_plugin(d).problems)


def test_contract_reports_an_invalid_manifest(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "plugin.json").write_text("{}")
    report = check_plugin(d)
    assert not report.ok and report.problems[0].startswith("manifest")


@pytest.mark.parametrize("kind", ["tool", "channel"])
def test_scaffolded_plugins_pass_the_contract(tmp_path, kind):
    result = runner.invoke(app, ["plugin", "new", f"demo-{kind}", "--kind", kind, "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    plugin_dir = tmp_path / f"demo-{kind}"
    assert_plugin_contract(plugin_dir)
    tested = runner.invoke(app, ["plugin", "test", str(plugin_dir), "--contract-only"])
    assert tested.exit_code == 0, tested.output


def test_plugin_test_runs_the_plugins_own_tests(tmp_path):
    runner.invoke(app, ["plugin", "new", "own-tests", "--dir", str(tmp_path)])
    result = runner.invoke(app, ["plugin", "test", str(tmp_path / "own-tests")])
    assert result.exit_code == 0, result.output


def test_plugin_test_fails_on_contract_problems(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "plugin.json").write_text("{}")
    assert runner.invoke(app, ["plugin", "test", str(d)]).exit_code == 1


def test_new_refuses_bad_ids_and_existing_dirs(tmp_path):
    assert runner.invoke(app, ["plugin", "new", "Bad_ID", "--dir", str(tmp_path)]).exit_code == 2
    runner.invoke(app, ["plugin", "new", "dup", "--dir", str(tmp_path)])
    assert runner.invoke(app, ["plugin", "new", "dup", "--dir", str(tmp_path)]).exit_code == 1


def test_enable_disable_and_list():
    from mira_engine.plugins.state import PluginState

    assert runner.invoke(app, ["plugin", "disable", "discord"]).exit_code == 0
    assert PluginState().enabled_override("discord") is False
    assert runner.invoke(app, ["plugin", "enable", "discord"]).exit_code == 0
    assert PluginState().enabled_override("discord") is True
    listed = runner.invoke(app, ["plugin", "list"])
    assert listed.exit_code == 0 and "discord" in listed.output
