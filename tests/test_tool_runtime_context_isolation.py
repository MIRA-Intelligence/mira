"""Concurrent turns must not see each other's tool routing or project context (#150)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.tools.consult import ConsultRoleTool
from mira_engine.agent.tools.cron import CronTool
from mira_engine.agent.tools.message import MessageTool
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.agent.tools.spawn import SpawnTool
from mira_engine.projects import ProjectRef


class _FakeManager:
    def __init__(self) -> None:
        self.spawned: list[dict[str, Any]] = []
        self.consulted: list[dict[str, Any]] = []

    async def spawn(self, **kwargs: Any) -> str:
        self.spawned.append(kwargs)
        return "spawned"

    async def consult(self, **kwargs: Any) -> tuple[str, str]:
        self.consulted.append(kwargs)
        return "ok", "s1"


class _FakeCron:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add_job(self, **kwargs: Any) -> Any:
        self.added.append(kwargs)
        return SimpleNamespace(id="job-1", name=kwargs["name"])


def _loop_with_tools() -> tuple[Any, _FakeManager, _FakeCron, list[Any]]:
    manager, cron, sent = _FakeManager(), _FakeCron(), []

    async def _send(msg: Any) -> None:
        sent.append(msg)

    tools = ToolRegistry()
    for tool in (SpawnTool(manager), ConsultRoleTool(manager), CronTool(cron), MessageTool(_send)):
        tools.register(tool)
    loop = SimpleNamespace(
        tools=tools,
        restrict_to_workspace=False,
        _skill_access_dirs_for_workspace=lambda _workspace: [],
    )
    return loop, manager, cron, sent


@pytest.mark.asyncio
async def test_project_free_turn_does_not_inherit_other_sessions_project(tmp_path: Path) -> None:
    loop, manager, cron, sent = _loop_with_tools()
    project = ProjectRef(project_id="p1", project_dir=tmp_path / "p1", metadata={})
    project_set, plain_set = asyncio.Event(), asyncio.Event()

    async def project_turn() -> None:
        await plain_set.wait()
        BaseAgentLoop._set_tool_context(
            loop, "ui", "chat-a", "turn-a", project_ref=project, session_key="ui:chat-a@p1",
        )
        project_set.set()

    async def plain_turn() -> None:
        BaseAgentLoop._set_tool_context(loop, "feishu", "chat-b", None, session_key="feishu:chat-b")
        plain_set.set()
        await project_set.wait()
        await loop.tools.get("spawn").execute(task="t")
        await loop.tools.get("consult").execute(role="critic", task="t")
        await loop.tools.get("cron").execute(action="add", message="m", every_seconds=60)
        await loop.tools.get("message").execute(content="hi")

    await asyncio.wait_for(
        asyncio.gather(asyncio.create_task(project_turn()), asyncio.create_task(plain_turn())), 5,
    )

    assert manager.spawned[-1]["workspace"] is None
    assert manager.spawned[-1]["project_id"] is None
    assert manager.spawned[-1]["session_key"] == "feishu:chat-b"
    assert manager.consulted[-1]["workspace"] is None
    assert cron.added[-1]["project_id"] is None
    assert cron.added[-1]["project_dir"] is None
    assert cron.added[-1]["channel"] == "feishu"
    assert sent[-1].chat_id == "chat-b"
    assert sent[-1].metadata["message_id"] is None  # not the project turn's "turn-a"


@pytest.mark.asyncio
async def test_single_turn_and_out_of_turn_callers_keep_current_behaviour(tmp_path: Path) -> None:
    loop, manager, cron, sent = _loop_with_tools()
    project = ProjectRef(project_id="p1", project_dir=tmp_path / "p1", metadata={})

    async def turn() -> None:
        BaseAgentLoop._set_tool_context(
            loop, "ui", "chat-a", "turn-a", project_ref=project, session_key="ui:chat-a@p1",
        )
        await loop.tools.get("spawn").execute(task="t")

    await asyncio.create_task(turn())
    assert manager.spawned[-1]["workspace"] == project.project_dir
    assert manager.spawned[-1]["project_id"] == "p1"

    # A caller outside any turn still falls back to the last context set.
    await loop.tools.get("spawn").execute(task="t2")
    assert manager.spawned[-1]["workspace"] == project.project_dir
