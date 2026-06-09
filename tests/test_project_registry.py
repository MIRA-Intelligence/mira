import json
from pathlib import Path

import pytest

from mira_engine.projects import ProjectRegistry, slugify_project_id, validate_project_id


def test_project_registry_persists_custom_project_dir(tmp_path: Path) -> None:
    workspace_file = tmp_path / ".mira" / "workspace.json"
    legacy_index = tmp_path / ".mira" / "ui" / "project-dirs.json"
    projects_root = tmp_path / "default-projects"
    chosen_parent = tmp_path / "chosen"

    registry = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )
    ref = registry.create_project(
        project_id="my-study",
        display_name="My Study",
        project_parent_dir=chosen_parent,
    )

    assert ref.project_id == "my-study"
    assert ref.project_dir == (chosen_parent / "my-study").resolve()
    assert (ref.project_dir / ".mira" / "project.json").is_file()

    payload = json.loads(workspace_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["projects"][0]["id"] == "my-study"
    assert payload["projects"][0]["project_dir"] == str(ref.project_dir)

    restarted = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )
    assert restarted.resolve("my-study").project_dir == ref.project_dir


def test_project_registry_can_hide_legacy_project_without_deleting_files(tmp_path: Path) -> None:
    workspace_file = tmp_path / ".mira" / "workspace.json"
    legacy_index = tmp_path / ".mira" / "ui" / "project-dirs.json"
    projects_root = tmp_path / "default-projects"
    project_dir = projects_root / "PRJ-HIDE"
    project_dir.mkdir(parents=True)

    registry = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )
    assert [ref.project_id for ref in registry.list_projects()] == ["PRJ-HIDE"]

    registry.drop_project_dir_registration("PRJ-HIDE", hide=True)

    assert project_dir.is_dir()
    assert registry.list_projects() == []

    restarted = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )
    assert restarted.list_projects() == []

    restarted.register_project_dir("PRJ-HIDE", project_dir)
    assert [ref.project_id for ref in restarted.list_projects()] == ["PRJ-HIDE"]


def test_project_registry_supports_unicode_project_names(tmp_path: Path) -> None:
    registry = ProjectRegistry(
        tmp_path / "default",
        workspace_path=tmp_path / "workspace.json",
        legacy_index_path=tmp_path / "index.json",
    )

    ref = registry.create_project(
        project_id="医学影像项目",
        display_name="医学影像项目",
    )

    assert ref.project_id == "医学影像项目"
    assert ref.project_dir.name == "医学影像项目"


def test_project_id_validation_rejects_path_separators() -> None:
    with pytest.raises(ValueError):
        validate_project_id("../outside")

    with pytest.raises(ValueError):
        validate_project_id("alpha:ui:PRJ-X")


def test_slugify_project_id_preserves_readable_name() -> None:
    assert slugify_project_id("  Lung CT Baseline  ") == "lung-ct-baseline"
    assert slugify_project_id("医学影像 项目") == "医学影像-项目"
    assert slugify_project_id("Alpha: Beta") == "alpha-beta"
