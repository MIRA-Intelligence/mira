import json
from pathlib import Path

import pytest

from mira_engine.projects import (
    ProjectDirectoryExistsError,
    ProjectRegistry,
    slugify_project_id,
    validate_project_id,
)


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


def test_project_registry_merges_updates_from_stale_instances(tmp_path: Path) -> None:
    workspace_file = tmp_path / ".mira" / "workspace.json"
    legacy_index = tmp_path / ".mira" / "ui" / "project-dirs.json"
    projects_root = tmp_path / "default-projects"
    project_a = tmp_path / "external" / "a"
    project_b = tmp_path / "external" / "b"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)

    first = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )
    second = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )

    first.register_project_dir("a", project_a)
    second.register_project_dir("b", project_b)

    payload = json.loads(workspace_file.read_text(encoding="utf-8"))
    assert {project["id"] for project in payload["projects"]} == {"a", "b"}
    assert set(json.loads(legacy_index.read_text(encoding="utf-8"))) == {"a", "b"}


def test_project_registry_concurrent_delete_is_not_resurrected(tmp_path: Path) -> None:
    workspace_file = tmp_path / ".mira" / "workspace.json"
    legacy_index = tmp_path / ".mira" / "ui" / "project-dirs.json"
    projects_root = tmp_path / "default-projects"
    project_a = tmp_path / "external" / "a"
    project_b = tmp_path / "external" / "b"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)

    seed = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )
    seed.register_project_dir("a", project_a)
    stale = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )
    remover = ProjectRegistry(
        projects_root,
        workspace_path=workspace_file,
        legacy_index_path=legacy_index,
    )

    remover.drop_project_dir_registration("a")
    stale.register_project_dir("b", project_b)

    payload = json.loads(workspace_file.read_text(encoding="utf-8"))
    assert {project["id"] for project in payload["projects"]} == {"b"}


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


@pytest.mark.parametrize("with_files", [False, True])
def test_existing_directory_requires_confirmation_and_preserves_files(tmp_path: Path, with_files: bool) -> None:
    registry = ProjectRegistry(
        tmp_path / "projects",
        workspace_path=tmp_path / "workspace.json",
        legacy_index_path=tmp_path / "index.json",
    )
    target = tmp_path / "chosen" / "study"
    target.mkdir(parents=True)
    if with_files:
        (target / "data").mkdir()
        (target / "data" / "scan.nii.gz").write_bytes(b"original scan\x00\xff")
        (target / "notes.md").write_text("User research notes")
        (target / ".mira").mkdir()
        (target / ".mira" / "user-notes.txt").write_text("Keep hidden files too")
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}

    with pytest.raises(ProjectDirectoryExistsError) as error:
        registry.create_project(project_id="study", display_name="Study", project_parent_dir=target.parent)
    assert error.value.project_dir == target
    assert not registry.workspace_path.exists()
    assert not (target / ".mira" / "project.json").exists()

    ref = registry.create_project(
        project_id="study", display_name="Study", project_parent_dir=target.parent,
        allow_existing_dir=True,
    )
    assert ref.project_dir == target
    assert all((target / path).read_bytes() == data for path, data in before.items())
    restarted = ProjectRegistry(
        registry.projects_root,
        workspace_path=registry.workspace_path,
        legacy_index_path=registry.legacy_index_path,
    )
    assert restarted.resolve("study").project_dir == target
    with pytest.raises(ProjectDirectoryExistsError):
        restarted.create_project(project_id="study", display_name="Study", project_dir=target)
    reopened = restarted.create_project(
        project_id="study", display_name="Study", project_dir=target, allow_existing_dir=True,
    )
    assert reopened.project_dir == target
    assert len(restarted.list_projects()) == 1
    assert all((target / path).read_bytes() == data for path, data in before.items())


def test_reusing_directory_does_not_overwrite_another_projects_metadata(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "projects", workspace_path=tmp_path / "workspace.json",
                               legacy_index_path=tmp_path / "index.json")
    target = tmp_path / "chosen" / "study"
    meta_path = target / ".mira" / "project.json"
    meta_path.parent.mkdir(parents=True)
    original = b'{"id": "another-project", "display_name": "Original"}'
    meta_path.write_bytes(original)
    with pytest.raises(FileExistsError):
        registry.create_project(project_id="study", display_name="Study", project_dir=target,
                                allow_existing_dir=True)
    assert meta_path.read_bytes() == original
    assert not registry.workspace_path.exists()


def test_reusing_directory_rejects_a_regular_file(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "projects", workspace_path=tmp_path / "workspace.json",
                               legacy_index_path=tmp_path / "index.json")
    target = tmp_path / "study"
    target.write_bytes(b"keep this file")
    with pytest.raises(FileExistsError):
        registry.create_project(project_id="study", display_name="Study", project_dir=target,
                                allow_existing_dir=True)
    assert target.read_bytes() == b"keep this file"
