from __future__ import annotations

from pathlib import Path

from mira_engine.agent.tools.spawn import SpawnTool


class _FakeManager:
    def __init__(self):
        self.calls = []

    async def spawn(self, **kwargs):
        self.calls.append(kwargs)
        return "spawned"


def test_spawn_tool_metadata() -> None:
    tool = SpawnTool(_FakeManager())
    assert tool.name == "spawn"
    assert "background" in tool.description
    assert tool.parameters["required"] == ["task"]


async def test_spawn_tool_execute_uses_default_context() -> None:
    manager = _FakeManager()
    tool = SpawnTool(manager)
    result = await tool.execute(task="do work")
    assert result == "spawned"
    assert manager.calls[0]["origin_channel"] == "cli"
    assert manager.calls[0]["origin_chat_id"] == "direct"
    assert manager.calls[0]["session_key"] == "cli:direct"


async def test_spawn_tool_execute_uses_updated_context_and_label() -> None:
    manager = _FakeManager()
    tool = SpawnTool(manager)
    tool.set_context("ui", "PRJ-2")
    await tool.execute(task="build", label="Batch")
    call = manager.calls[0]
    assert call["origin_channel"] == "ui"
    assert call["origin_chat_id"] == "PRJ-2"
    assert call["session_key"] == "ui:PRJ-2"
    assert call["label"] == "Batch"


async def test_spawn_tool_forwards_scoped_session_and_project_context(tmp_path: Path) -> None:
    manager = _FakeManager()
    tool = SpawnTool(manager)
    project_dir = tmp_path / "alpha"

    tool.set_context("ui", "chat-1")
    tool.set_session_key("alpha:ui:chat-1")
    tool.set_project_context("alpha", str(project_dir))
    await tool.execute(task="run checks")

    call = manager.calls[0]
    assert call["session_key"] == "alpha:ui:chat-1"
    assert call["project_id"] == "alpha"
    assert call["workspace"] == project_dir


async def test_spawn_tool_can_clear_project_context(tmp_path: Path) -> None:
    manager = _FakeManager()
    tool = SpawnTool(manager)
    project_dir = tmp_path / "alpha"

    tool.set_project_context("alpha", str(project_dir))
    tool.clear_project_context()
    await tool.execute(task="global")

    call = manager.calls[0]
    assert call["project_id"] is None
    assert call["workspace"] is None
