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


def _create_builtin_tree(tmp_path: Path) -> Path:
    root = tmp_path / "builtin-skills"
    (root / "research" / "finder").mkdir(parents=True)
    (root / "research" / "finder" / "SKILL.md").write_text("# finder", encoding="utf-8")
    (root / "engineering" / "builder").mkdir(parents=True)
    (root / "engineering" / "builder" / "SKILL.md").write_text("# builder", encoding="utf-8")
    return root


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
    assert {item["id"] for item in plugins} >= {"builtin-skills", "dl-pack"}
    plugin = next(item for item in plugins if item["id"] == "dl-pack")
    assert plugin["id"] == "dl-pack"
    assert plugin["enabled"]["effective"] is True
    assert {s["id"] for s in plugin["skills"]} == {"trainer", "evaluator"}

    manager.set_enabled(
        scope="global",
        plugin_id="dl-pack",
        target_type="plugin",
        enabled=False,
    )
    updated_plugins = manager.list_plugins()
    assert next(item for item in updated_plugins if item["id"] == "dl-pack")["enabled"]["effective"] is False

    manager.set_enabled(
        scope="project",
        plugin_id="dl-pack",
        target_type="plugin",
        enabled=True,
    )
    updated_plugins = manager.list_plugins()
    assert next(item for item in updated_plugins if item["id"] == "dl-pack")["enabled"]["effective"] is True

    manager.set_enabled(
        scope="project",
        plugin_id="dl-pack",
        target_type="group",
        target_id="deep-learning",
        enabled=False,
    )
    enabled_names = {item["name"] for item in manager.list_enabled_skills()}
    assert "trainer" not in enabled_names
    assert "evaluator" not in enabled_names


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
    remaining = manager.list_plugins()
    assert [item["id"] for item in remaining] == ["builtin-skills"]
    with pytest.raises(SkillPluginError):
        manager.uninstall("vision-pack")


def test_builtin_skill_groups_toggle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    manager = SkillPluginManager(tmp_path / "project")
    manager.builtin_skills_dir = _create_builtin_tree(tmp_path)

    plugins = manager.list_plugins()
    builtin = next(item for item in plugins if item["id"] == "builtin-skills")
    assert {group["id"] for group in builtin["groups"]} == {"engineering", "research"}

    manager.set_enabled(
        scope="project",
        plugin_id="builtin-skills",
        target_type="group",
        target_id="research",
        enabled=False,
    )
    enabled_names = {item["name"] for item in manager.list_enabled_skills()}
    assert "finder" not in enabled_names
    assert "builder" in enabled_names
