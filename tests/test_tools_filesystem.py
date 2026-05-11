from __future__ import annotations

from pathlib import Path

from mira_engine.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from mira_engine.agent.tools.search import GlobTool


async def test_read_file_success_and_missing(tmp_path: Path) -> None:
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    assert await tool.execute("note.txt") == "hello"
    assert await tool.execute("missing.txt") == "Error: File not found: missing.txt"


async def test_read_file_rejects_directory_and_large_content(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    tool = ReadFileTool(workspace=tmp_path)
    assert await tool.execute("dir") == "Error: Not a file: dir"

    huge = tmp_path / "huge.txt"
    huge.write_text("x" * 50, encoding="utf-8")
    tool._MAX_CHARS = 10
    msg = await tool.execute("huge.txt")
    assert "File too large" in msg


async def test_read_file_truncates_long_text(tmp_path: Path) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("a" * 25, encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    tool._MAX_CHARS = 20

    output = await tool.execute("payload.txt")
    assert output.startswith("a" * 20)
    assert "truncated" in output


async def test_write_file_creates_parent_and_writes_content(tmp_path: Path) -> None:
    tool = WriteFileTool(workspace=tmp_path)
    result = await tool.execute("nested/file.txt", "abc")
    assert "Successfully wrote 3 bytes" in result
    assert (tmp_path / "nested" / "file.txt").read_text(encoding="utf-8") == "abc"


async def test_edit_file_success_and_not_found_paths(tmp_path: Path) -> None:
    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute("missing.txt", "a", "b")
    assert result == "Error: File not found: missing.txt"

    f = tmp_path / "f.txt"
    f.write_text("hello world", encoding="utf-8")
    ok = await tool.execute("f.txt", "world", "mira")
    assert ok.startswith("Successfully edited")
    assert f.read_text(encoding="utf-8") == "hello mira"


async def test_edit_file_warns_when_multiple_occurrences(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x\ny\nx\n", encoding="utf-8")
    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute("f.txt", "x", "z")
    assert "appears 2 times" in result


def test_edit_file_not_found_message_with_best_match() -> None:
    content = "line one\nline two\nline three\n"
    msg = EditFileTool._not_found_message("line one\nline two\nline thre\n", content, "demo.txt")
    assert "Best match" in msg
    assert "demo.txt" in msg


async def test_list_dir_handles_empty_and_files(tmp_path: Path) -> None:
    tool = ListDirTool(workspace=tmp_path)
    empty = await tool.execute(".")
    assert "is empty" in empty

    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    listing = await tool.execute(".")
    assert "📄 a.txt" in listing
    assert "📁 sub" in listing


async def test_list_dir_error_conditions(tmp_path: Path) -> None:
    tool = ListDirTool(workspace=tmp_path)
    assert await tool.execute("missing") == "Error: Directory not found: missing"
    file_path = tmp_path / "file.txt"
    file_path.write_text("a", encoding="utf-8")
    assert await tool.execute("file.txt") == "Error: Not a directory: file.txt"


async def test_filesystem_tools_use_runtime_project_context(tmp_path: Path) -> None:
    instance_workspace = tmp_path / "instance"
    project_a = tmp_path / "projects" / "alpha"
    project_b = tmp_path / "projects" / "beta"
    instance_workspace.mkdir()
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)
    (instance_workspace / "note.txt").write_text("instance", encoding="utf-8")
    (project_a / "note.txt").write_text("alpha", encoding="utf-8")
    (project_b / "note.txt").write_text("beta", encoding="utf-8")

    tool = ReadFileTool(workspace=instance_workspace, allowed_dir=instance_workspace)
    assert await tool.execute("note.txt") == "instance"

    tool.set_runtime_context(workspace=project_a, allowed_dir=project_a)
    assert await tool.execute("note.txt") == "alpha"
    assert "outside allowed directories" in await tool.execute(str(project_b / "note.txt"))

    tool.clear_runtime_context()
    assert await tool.execute("note.txt") == "instance"


async def test_glob_display_paths_are_relative_to_runtime_project(tmp_path: Path) -> None:
    instance_workspace = tmp_path / "instance"
    project_dir = tmp_path / "projects" / "alpha"
    (project_dir / "src").mkdir(parents=True)
    instance_workspace.mkdir()
    (project_dir / "src" / "main.py").write_text("print('ok')", encoding="utf-8")

    tool = GlobTool(workspace=instance_workspace, allowed_dir=instance_workspace)
    tool.set_runtime_context(workspace=project_dir, allowed_dir=project_dir)

    assert await tool.execute("*.py", path="src") == "src/main.py"
