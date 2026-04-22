import json
import zipfile
from pathlib import Path

import pytest

from mira_engine.agent.skill_plugins import SkillPluginError, SkillPluginManager
from mira_engine.agent import skill_plugins as skill_plugins_mod


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

    with pytest.raises(SkillPluginError):
        manager.set_enabled(
            scope="global",
            plugin_id="dl-pack",
            target_type="plugin",
            enabled=False,
        )

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

    # Skill-level override makes group gating ineffective for that skill.
    manager.set_enabled(
        scope="project",
        plugin_id="dl-pack",
        target_type="skill",
        target_id="trainer",
        enabled=True,
    )
    enabled_names = {item["name"] for item in manager.list_enabled_skills()}
    assert "trainer" in enabled_names
    plugin = next(item for item in manager.list_plugins() if item["id"] == "dl-pack")
    group = next(item for item in plugin["groups"] if item["id"] == "deep-learning")
    assert group["customized"]["project"] is True

    # Clicking group again should restore group-level control (clear skill overrides).
    manager.set_enabled(
        scope="project",
        plugin_id="dl-pack",
        target_type="group",
        target_id="deep-learning",
        enabled=False,
    )
    enabled_names = {item["name"] for item in manager.list_enabled_skills()}
    assert "trainer" not in enabled_names


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


def test_install_from_zip_without_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    project_workspace = tmp_path / "project"
    manager = SkillPluginManager(project_workspace)

    src = tmp_path / "no-manifest-package"
    (src / "research" / "lit-search").mkdir(parents=True)
    (src / "research" / "lit-search" / "SKILL.md").write_text(
        "---\nname: Literature Search\n---\n\n# lit",
        encoding="utf-8",
    )

    archive = tmp_path / "local-skill-pack.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for item in src.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(src))

    installed = manager.install_from_zip(archive)
    assert installed["id"] == "local-skill-pack"

    plugins = manager.list_plugins()
    inferred = next(item for item in plugins if item["id"] == "local-skill-pack")
    assert any(group["id"] == "research" for group in inferred["groups"])
    assert any(skill["id"] == "literature-search" for skill in inferred["skills"])
    assert any(skill["name"] == "Literature Search" for skill in inferred["skills"])


def test_install_from_zip_without_manifest_with_wrapper_dir_keeps_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    manager = SkillPluginManager(tmp_path / "project")

    src = tmp_path / "package-root"
    (src / "research" / "lit-search" / "SKILL.md").parent.mkdir(parents=True)
    (src / "research" / "lit-search" / "SKILL.md").write_text(
        "---\nname: Literature Search\n---\n\n# lit",
        encoding="utf-8",
    )

    archive = tmp_path / "wrapped-pack.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for item in src.rglob("*"):
            if item.is_file():
                zf.write(item, Path("outer-folder") / item.relative_to(src))

    installed = manager.install_from_zip(archive)
    assert installed["id"] == "wrapped-pack"
    plugin = next(item for item in manager.list_plugins() if item["id"] == "wrapped-pack")
    assert any(group["id"] == "research" for group in plugin["groups"])
    assert any(skill["id"] == "literature-search" for skill in plugin["skills"])


def test_install_from_zip_without_manifest_single_skill_no_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    manager = SkillPluginManager(tmp_path / "project")

    src = tmp_path / "single-skill-package"
    (src / "my-skill").mkdir(parents=True)
    (src / "my-skill" / "SKILL.md").write_text(
        "---\nname: My Skill\n---\n\ncontent",
        encoding="utf-8",
    )

    archive = tmp_path / "single-skill-package.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for item in src.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(src))

    installed = manager.install_from_zip(archive)
    assert installed["id"] == "single-skill-package"
    plugin = next(item for item in manager.list_plugins() if item["id"] == "single-skill-package")
    assert plugin["groups"] == []
    assert len(plugin["skills"]) == 1
    assert plugin["skills"][0]["name"] == "My Skill"


def test_legacy_manifest_without_groups_infers_group_from_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    manager = SkillPluginManager(tmp_path / "project")

    src = tmp_path / "legacy-grouped"
    (src / "research" / "finder").mkdir(parents=True)
    (src / "research" / "finder" / "SKILL.md").write_text("# Finder", encoding="utf-8")
    (src / "plugin.json").write_text(
        json.dumps({
            "id": "legacy-grouped",
            "version": "0.1.0",
            "skills": [{"id": "finder", "path": "research/finder"}],
        }),
        encoding="utf-8",
    )

    manager.install_from_directory(src)
    plugin = next(item for item in manager.list_plugins() if item["id"] == "legacy-grouped")
    assert any(group["id"] == "research" for group in plugin["groups"])
    finder = next(skill for skill in plugin["skills"] if skill["id"] == "finder")
    assert finder["group_ids"] == ["research"]


def test_legacy_manifest_without_groups_keeps_skills_prefix_ungrouped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_global_workspace(monkeypatch, tmp_path)
    manager = SkillPluginManager(tmp_path / "project")

    src = tmp_path / "legacy-plain"
    (src / "skills" / "writer").mkdir(parents=True)
    (src / "skills" / "writer" / "SKILL.md").write_text("# Writer", encoding="utf-8")
    (src / "plugin.json").write_text(
        json.dumps({
            "id": "legacy-plain",
            "version": "0.1.0",
            "skills": [{"id": "writer", "path": "skills/writer"}],
        }),
        encoding="utf-8",
    )

    manager.install_from_directory(src)
    plugin = next(item for item in manager.list_plugins() if item["id"] == "legacy-plain")
    assert plugin["groups"] == []
    writer = next(skill for skill in plugin["skills"] if skill["id"] == "writer")
    assert writer["group_ids"] == []


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

    with pytest.raises(SkillPluginError):
        manager.set_enabled(
            scope="project",
            plugin_id="builtin-skills",
            target_type="plugin",
            enabled=False,
        )
