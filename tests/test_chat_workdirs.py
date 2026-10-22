"""Quick Chat working directories and the projects/ layout."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.agent.tools.shell import ExecTool
from mira_engine.chats import chat_workdir, ensure_chat_workdir
from mira_engine.projects import ProjectRegistry


def _registry(root: Path) -> ProjectRegistry:
    return ProjectRegistry(
        root,
        workspace_path=root.parent / "state" / "workspace.json",
        legacy_index_path=root.parent / "state" / "project-dirs.json",
    )


def test_chat_workdir_is_under_chats(tmp_path: Path) -> None:
    assert chat_workdir(tmp_path, "chat-1") == (tmp_path / "chats" / "chat-1").resolve()
    assert not (tmp_path / "chats").exists()
    created = ensure_chat_workdir(tmp_path, "chat-1")
    assert created.is_dir()


@pytest.mark.parametrize("legacy_parent", [Path(".mira") / "projects", Path("projects")])
def test_chat_keeps_a_folder_the_model_already_used(tmp_path: Path, legacy_parent: Path) -> None:
    legacy = tmp_path / legacy_parent / "chat-1"
    legacy.mkdir(parents=True)
    assert ensure_chat_workdir(tmp_path, "chat-1") == legacy.resolve()
    assert not (tmp_path / "chats" / "chat-1").exists()


@pytest.mark.parametrize("bad_id", ["", "..", "a/b", "a\\b"])
def test_chat_workdir_rejects_ids_that_are_not_one_folder(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(ValueError):
        chat_workdir(tmp_path, bad_id)


def test_new_projects_go_under_projects(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    registry = _registry(root)

    created = registry.create_project(project_id="PRJ-new", display_name="New")
    resolved = registry.resolve("PRJ-auto", create=True)

    assert created.project_dir == (root / "projects" / "PRJ-new").resolve()
    assert resolved.project_dir == (root / "projects" / "PRJ-auto").resolve()
    assert registry.default_project_parent == root.resolve() / "projects"


def test_projects_in_the_old_layout_keep_working(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    old = root / "PRJ-0001"
    old.mkdir(parents=True)
    registry = _registry(root)

    assert registry.resolve("PRJ-0001").project_dir == old.resolve()
    assert registry.resolve("PRJ-0001", create=True).project_dir == old.resolve()
    assert not (root / "projects" / "PRJ-0001").exists()
    (root / "projects" / "PRJ-0002").mkdir(parents=True)
    assert {ref.project_id for ref in registry.list_projects()} == {"PRJ-0001", "PRJ-0002"}

    restarted = _registry(root)
    assert restarted.resolve("PRJ-0001").project_dir == old.resolve()


def test_exec_starts_in_the_chat_folder_but_keeps_the_workspace_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chat_dir = workspace / "chats" / "chat-1"
    chat_dir.mkdir(parents=True)
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)

    tool.set_default_cwd(chat_dir)
    assert tool._default_cwd() == str(chat_dir)
    assert tool._workspace() == str(workspace)
    assert tool._working_dir_error(str(workspace / "data")) is None
    assert tool._working_dir_error("..") is None  # chats/, still in the workspace
    assert tool._working_dir_error(str(tmp_path)) is not None

    tool.clear_runtime_context()
    assert tool._default_cwd() == str(workspace)


async def test_exec_runs_relative_commands_in_the_chat_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    chat_dir = workspace / "chats" / "chat-1"
    chat_dir.mkdir(parents=True)
    tool = ExecTool(working_dir=str(workspace))
    tool.set_default_cwd(chat_dir)

    # Not `pwd`: under Git bash on Windows it prints an MSYS path (/c/Users/...).
    result = await tool.execute(f'"{sys.executable}" -c "import os; print(os.getcwd())"')

    assert str(chat_dir.resolve()) in result


def test_loop_reads_chat_dir_only_when_the_ui_created_it(tmp_path: Path) -> None:
    chat_dir = tmp_path / "chats" / "chat-1"
    assert BaseAgentLoop._chat_dir_from_metadata({"chat_dir": str(chat_dir)}) is None
    chat_dir.mkdir(parents=True)
    assert BaseAgentLoop._chat_dir_from_metadata({"chat_dir": str(chat_dir)}) == chat_dir
    assert BaseAgentLoop._chat_dir_from_metadata({}) is None
    assert BaseAgentLoop._chat_dir_from_metadata(None) is None


def test_quick_chat_turn_points_exec_at_the_chat_folder_and_the_next_turn_resets_it(tmp_path: Path) -> None:
    tools = ToolRegistry()
    exec_tool = ExecTool(working_dir=str(tmp_path))
    tools.register(exec_tool)
    loop = SimpleNamespace(tools=tools, restrict_to_workspace=False)
    chat_dir = tmp_path / "chats" / "chat-1"

    BaseAgentLoop._set_tool_context(loop, "ui", "chat-1", chat_dir=chat_dir)
    assert exec_tool._default_cwd() == str(chat_dir)

    BaseAgentLoop._set_tool_context(loop, "cli", "direct")
    assert exec_tool._default_cwd() == str(tmp_path)
