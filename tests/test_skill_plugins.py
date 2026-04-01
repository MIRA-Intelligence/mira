import json
import zipfile
from pathlib import Path

import pytest

from medpilot.agent.skill_plugins import SkillPluginError, SkillPluginManager
from medpilot.agent import skill_plugins as skill_plugins_mod


def _write_skill(base: Path, name: str, body: str = "# skill\n") -> None:
    skill_dir = base / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _write_manifest(
    plugin_dir: Path,
    *,
    plugin_id: str = "dl-pack",
    include_groups: bool = True,
) -> None:
    payload: dict[str, object] = {
        "id": plugin_id,
        "name": "DL Pack",
        "version": "1.0.0",
        "skills": [
            {"id": "trainer", "path": "skills/trainer"},
            {"id": "evaluator", "path": "skills/evaluator"},
        ],
    }
    if include_groups:
        payload["groups"] = [{"id": "deep-learning", "skills": ["trainer", "evaluator"]}]
    (plugin_dir / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")


def _create_plugin_source(tmp_path: Path, plugin_id: str = "dl-pack") -> Path:
    plugin_dir = tmp_path / "plugin-src"
    plugin_dir.mkdir(parents=True)
    _write_skill(plugin_dir, "trainer")
    _write_skill(plugin_dir, "evaluator")
    _write_manifest(plugin_dir, plugin_id=plugin_id)
    return plugin_dir


def _patch_global_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    global_workspace = tmp_path / "global-workspace"
    global_workspace.mkdir(parents=True)
    monkeypatch.setattr(skill_plugins_mod, "get_workspace_path", lambda _workspace: global_workspace)
    return global_workspace


def test_install_and_scope_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    project_workspace = tmp_path / "project"
    plugin_source = _create_plugin_source(tmp_path)
    manager = SkillPluginManager(project_workspace)

    manager.install_from_directory(plugin_source)
    plugins = manager.list_plugins()
    assert len(plugins) == 1
    plugin = plugins[0]
    assert plugin["id"] == "dl-pack"
    assert plugin["enabled"]["effective"] is True
    assert {s["id"] for s in plugin["skills"]} == {"trainer", "evaluator"}

    manager.set_enabled(
        scope="global",
        plugin_id="dl-pack",
        target_type="plugin",
        enabled=False,
    )
    assert manager.list_plugins()[0]["enabled"]["effective"] is False

    manager.set_enabled(
        scope="project",
        plugin_id="dl-pack",
        target_type="plugin",
        enabled=True,
    )
    assert manager.list_plugins()[0]["enabled"]["effective"] is True

    manager.set_enabled(
        scope="project",
        plugin_id="dl-pack",
        target_type="group",
        target_id="deep-learning",
        enabled=False,
    )
    enabled_skills = manager.list_enabled_skills()
    assert enabled_skills == []


def test_install_from_zip_and_reject_traversal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    project_workspace = tmp_path / "project"
    plugin_source = _create_plugin_source(tmp_path)
    manager = SkillPluginManager(project_workspace)

    good_zip = tmp_path / "plugin.zip"
    with zipfile.ZipFile(good_zip, "w") as zf:
        for file in plugin_source.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(plugin_source))

    installed = manager.install_from_zip(good_zip)
    assert installed["id"] == "dl-pack"

    bad_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    with pytest.raises(SkillPluginError):
        manager.install_from_zip(bad_zip)


def test_uninstall_cleans_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    project_workspace = tmp_path / "project"
    plugin_source = _create_plugin_source(tmp_path, plugin_id="vision-pack")
    manager = SkillPluginManager(project_workspace)
    manager.install_from_directory(plugin_source)
    manager.set_enabled(
        scope="global",
        plugin_id="vision-pack",
        target_type="skill",
        target_id="trainer",
        enabled=False,
    )

    manager.uninstall("vision-pack")
    assert manager.list_plugins() == []
    with pytest.raises(SkillPluginError):
        manager.uninstall("vision-pack")
