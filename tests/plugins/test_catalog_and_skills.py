"""One plugin catalog: kernel plugins' skills reach the agent, the skill manager and the UI."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from mira_engine.agent import skill_plugins as skill_plugins_mod
from mira_engine.agent.skill_plugins import SkillPluginError, SkillPluginManager
from mira_engine.agent.skills import SkillsLoader
from mira_engine.plugins import catalog
from mira_engine.plugins.state import PluginState
from mira_engine.plugins.testing import check_plugin

SKILL = "---\nname: {name}\ndescription: {name} skill.\n---\n\n# {name}\n"


def _kernel_plugin(root: Path, pid: str, *, skills=("howto",), code: bool = False, **manifest) -> Path:
    d = root / pid
    d.mkdir(parents=True)
    provides: dict = {"skills": [{"id": s, "path": f"skills/{s}"} for s in skills]}
    for s in skills:
        (d / "skills" / s).mkdir(parents=True)
        (d / "skills" / s / "SKILL.md").write_text(SKILL.format(name=s))
    if code:
        provides["tools"] = [{"name": f"{pid.replace('-', '_')}_tool", "entry": "tool:T"}]
        (d / "tool.py").write_text("raise RuntimeError('plugin code must not be imported')\n")
    (d / "plugin.json").write_text(json.dumps({"id": pid, "version": "1.2.0", "provides": provides, **manifest}))
    return d


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    bundled, user, global_ws = tmp_path / "bundled", tmp_path / "user", tmp_path / "global-ws"
    for d in (bundled, user, global_ws):
        d.mkdir()
    monkeypatch.setattr(catalog, "bundled_plugins_dir", lambda: bundled)
    monkeypatch.setattr(catalog, "user_plugins_dir", lambda: user)
    monkeypatch.setattr("mira_engine.plugins.paths.user_plugins_dir", lambda: user)
    monkeypatch.setattr(skill_plugins_mod, "get_workspace_path", lambda _w: global_ws)
    return bundled, user


def _manager(tmp_path) -> SkillPluginManager:
    return SkillPluginManager(tmp_path / "project")


def test_catalog_reads_manifests_without_importing_plugin_code(dirs):
    bundled, user = dirs
    _kernel_plugin(bundled, "first-party", code=True)
    _kernel_plugin(user, "community-pack", code=True)
    _kernel_plugin(user, "first-party")  # a duplicate id: the bundled one wins
    entries = {e.id: e for e in catalog.entries()}
    assert set(entries) == {"first-party", "community-pack"}
    assert entries["first-party"].tier == "core-bundled" and entries["first-party"].active
    assert entries["community-pack"].tier == "experimental" and not entries["community-pack"].active


def test_catalog_follows_state_and_manifest_changes(dirs):
    _, user = dirs
    d = _kernel_plugin(user, "pack")
    assert not catalog.get("pack").active
    PluginState().set_enabled("pack", True)
    assert catalog.get("pack").active
    PluginState().set_remote({"pack": {"*": "bad release"}})
    assert not catalog.get("pack").active and "bad release" in catalog.get("pack").reason
    PluginState().set_remote({})
    PluginState().set_registry_tiers({"pack": "verified"})
    assert catalog.get("pack").tier == "verified"
    data = json.loads((d / "plugin.json").read_text())
    (d / "plugin.json").write_text(json.dumps({**data, "version": "2.0.0"}))
    import os

    os.utime(d / "plugin.json", ns=(1, 1))  # a different mtime even within the clock's resolution
    assert catalog.get("pack").manifest.version == "2.0.0"


def test_kernel_plugin_skills_are_listed_toggled_and_loaded(dirs, tmp_path):
    bundled, user = dirs
    _kernel_plugin(bundled, "bundled-skills", skills=("alpha",))
    _kernel_plugin(user, "user-skills", skills=("beta",), default_enabled=True)
    manager = _manager(tmp_path)
    plugins = {p["id"]: p for p in manager.list_plugins()}
    assert plugins["bundled-skills"]["source"]["type"] == "kernel"
    assert plugins["user-skills"]["version"] == "1.2.0"
    enabled = {s["name"] for s in manager.list_enabled_skills()}
    assert {"alpha", "beta"} <= enabled
    loader = SkillsLoader(tmp_path / "project", plugin_manager=manager)
    assert {"alpha", "beta"} <= {s["name"] for s in loader.list_skills(filter_unavailable=False)}

    # The usual per-skill toggles work for kernel plugins too.
    manager.set_enabled(scope="global", target_type="skill", plugin_id="user-skills", target_id="beta", enabled=False)
    assert "beta" not in {s["name"] for s in manager.list_enabled_skills()}


def test_a_switched_off_kernel_plugin_lists_with_its_skills_off(dirs, tmp_path):
    _, user = dirs
    _kernel_plugin(user, "dormant", skills=("gamma",))
    manager = _manager(tmp_path)
    plugin = next(p for p in manager.list_plugins() if p["id"] == "dormant")
    assert plugin["enabled"]["effective"] is False
    assert all(not s["enabled"]["effective"] for s in plugin["skills"])
    assert "gamma" not in {s["name"] for s in manager.list_enabled_skills()}
    PluginState().set_enabled("dormant", True)
    assert "gamma" in {s["name"] for s in _manager(tmp_path).list_enabled_skills()}


def test_uninstalling_kernel_plugins(dirs, tmp_path):
    bundled, user = dirs
    _kernel_plugin(bundled, "shipped")
    d = _kernel_plugin(user, "removable", default_enabled=True)
    PluginState().set_enabled("removable", True)
    manager = _manager(tmp_path)
    with pytest.raises(SkillPluginError, match="ships with Mira"):
        manager.uninstall("shipped")
    manager.uninstall("removable")
    assert not d.exists() and PluginState().entry("removable") == {}
    assert all(p["id"] != "removable" for p in manager.list_plugins())


def _zip_dir(src: Path) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for item in src.rglob("*"):
            if item.is_file():
                zf.write(item, Path(src.name) / item.relative_to(src))
    out = src.parent / f"{src.name}.zip"
    out.write_bytes(buf.getvalue())
    return out


def test_installing_a_package_with_code_makes_it_a_switched_off_plugin(dirs, tmp_path):
    _, user = dirs
    src = _kernel_plugin(tmp_path / "src", "coded", code=True)
    archive = _zip_dir(src)
    manager = _manager(tmp_path)
    installed = manager.install_from_zip(archive)
    assert installed["source"]["type"] == "kernel" and "mira plugin enable coded" in installed["note"]
    assert (user / "coded" / "plugin.json").is_file()
    assert not (manager.plugins_root / "coded").exists()
    assert PluginState().enabled_override("coded") is False
    with pytest.raises(SkillPluginError, match="contains code"):
        manager.install_from_zip(archive, allow_code=False)
    # A skills-only package with a manifest still installs as a skill pack.
    pack = _kernel_plugin(tmp_path / "src", "skills-only")
    assert manager.install_from_zip(_zip_dir(pack), allow_code=False)["source"]["type"] == "zip"


def test_contract_checks_provided_skill_paths(tmp_path):
    good = _kernel_plugin(tmp_path, "good-skills")
    assert check_plugin(good, "core-bundled").ok
    bad = _kernel_plugin(tmp_path, "bad-skills")
    data = json.loads((bad / "plugin.json").read_text())
    data["provides"]["skills"] = [{"id": "missing", "path": "skills/missing"}, {"id": "escape", "path": "../good-skills"}]
    (bad / "plugin.json").write_text(json.dumps(data))
    report = check_plugin(bad, "core-bundled")
    assert not report.ok
    assert any("no SKILL.md" in p for p in report.problems) and any("outside the plugin" in p for p in report.problems)


async def test_hub_install_refuses_packages_with_code(dirs, tmp_path, monkeypatch):
    import hashlib
    from functools import partial

    import httpx

    from mira_engine.agent.tools import hub as hub_mod
    from mira_engine.agent.tools.hub import HubInstallTool

    data = _zip_dir(_kernel_plugin(tmp_path / "src", "sneaky", code=True)).read_bytes()
    index = {"skills": [{"slug": "sneaky", "version": "1.0.0", "zip": "/mira-hub/registry/skills/sneaky.zip",
                         "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json=index)
        return httpx.Response(200, content=data)

    monkeypatch.setattr(hub_mod.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)))
    result = json.loads(await HubInstallTool(workspace=tmp_path / "project", hub_url="https://hub.test/mira-hub/").execute(slug="sneaky"))
    assert "contains code" in result["error"]
    _, user = dirs
    assert not (user / "sneaky").exists()


def test_plugin_list_shows_skill_packs(dirs, tmp_path):
    from typer.testing import CliRunner

    from mira_engine.cli.commands import app

    pack = tmp_path / "pack-src" / "lit-pack"
    (pack / "lit").mkdir(parents=True)
    (pack / "lit" / "SKILL.md").write_text(SKILL.format(name="lit"))
    SkillPluginManager(tmp_path / "project").install_from_directory(pack)
    out = CliRunner().invoke(app, ["plugin", "list"], terminal_width=200)
    assert out.exit_code == 0 and "lit-pack" in out.output and "skill pack" in out.output
