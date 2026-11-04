"""Long-term memory lives in MIRA_HOME/mind (Dream-only); project memory in <project>/memory."""

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira_engine.agent.memory import Dream, MemoryStore, migrate_to_mind_dir
from mira_engine.agent.runner import AgentRunResult
from mira_engine.agent.tools.filesystem import EditFileTool, WriteFileTool
from mira_engine.config.paths import get_mind_dir, get_workspace_path
from mira_engine.providers.base import LLMResponse, ToolCallRequest
from mira_engine.session.manager import Session


def _builtin(name: str) -> str:
    from importlib.resources import files as pkg_files

    return (pkg_files("mira_engine") / "templates" / name).read_text(encoding="utf-8")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_old_workspace_memory_moves_into_the_mind() -> None:
    ws = get_workspace_path(None)
    (ws / "memory").mkdir()
    (ws / ".mira" / "memory").mkdir(parents=True)
    (ws / "SOUL.md").write_text("# Soul\n\nTerse and direct.", encoding="utf-8")
    (ws / "USER.md").write_text(_builtin("USER.md"), encoding="utf-8")
    (ws / "memory" / "MEMORY.md").write_text("# Long-term Memory\n\n- Prefers Chinese replies", encoding="utf-8")
    (ws / "memory" / "history.jsonl").write_text('{"cursor": 4, "timestamp": "t", "content": "x"}\n', encoding="utf-8")
    (ws / ".mira" / "memory" / ".dream_cursor").write_text("3", encoding="utf-8")
    (ws / ".mira" / "memory" / "MEMORY.md").write_text("- Uses uv for Python", encoding="utf-8")
    (ws / ".mira" / "token-usage.sqlite3").write_text("", encoding="utf-8")

    moved = migrate_to_mind_dir(ws)

    mind = get_mind_dir()
    assert set(moved) == {"SOUL.md", "MEMORY.md", ".mira/memory/MEMORY.md", "history.jsonl"}
    assert (mind / "SOUL.md").read_text(encoding="utf-8") == "# Soul\n\nTerse and direct."
    assert not (mind / "USER.md").exists()  # an unedited template carries nothing
    memory = (mind / "MEMORY.md").read_text(encoding="utf-8")
    assert "Prefers Chinese replies" in memory and "Uses uv for Python" in memory
    store = MemoryStore(ws, project=False)
    assert store.get_last_dream_cursor() == 3
    assert [r["cursor"] for r in store.read_unprocessed_history(0)] == [4]
    for gone in ("SOUL.md", "USER.md", "memory/MEMORY.md", "memory/history.jsonl", ".mira/memory"):
        assert not (ws / gone).exists(), gone
    assert (ws / ".mira" / "token-usage.sqlite3").exists()
    assert migrate_to_mind_dir(ws) == []


def test_migration_never_overwrites_what_dream_wrote() -> None:
    ws = get_workspace_path(None)
    mind = get_mind_dir()
    (mind / "SOUL.md").write_text("Dream's soul", encoding="utf-8")
    (ws / "SOUL.md").write_text("old workspace soul", encoding="utf-8")

    assert migrate_to_mind_dir(ws) == []
    assert (mind / "SOUL.md").read_text(encoding="utf-8") == "Dream's soul"
    assert (ws / "SOUL.md").exists()


def test_project_history_is_mirrored_for_dream(tmp_path: Path) -> None:
    project = MemoryStore(tmp_path / "PRJ-0007", project=True)
    project.append_history("Chose focal loss; Dice 0.81")
    project.raw_archive([{"role": "user", "content": "raw dump", "timestamp": "2026-01-01T00:00"}])

    assert [r["content"] for r in _rows(project.history_jsonl_file)][0] == "Chose focal loss; Dice 0.81"
    mind_rows = _rows(get_mind_dir() / "history.jsonl")
    assert [r["content"] for r in mind_rows] == ["[project PRJ-0007] Chose focal loss; Dice 0.81"]


def _save_memory_provider(arguments: dict) -> MagicMock:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LLMResponse(
        content=None, tool_calls=[ToolCallRequest(id="1", name="save_memory", arguments=arguments)],
    ))
    return provider


async def test_project_consolidation_folds_pending_history(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "proj", project=True)
    store.append_history("[2026-01-01 10:00] archived earlier: dataset at /data/ct")
    store.write_long_term("# Project\n- goal: segment nodules")
    session = Session(key="k", messages=[{"role": "user", "content": "hi", "timestamp": "2026-01-01T11:00"}])
    provider = _save_memory_provider({
        "history_entry": "[2026-01-01 11:00] said hi",
        "project_memory_update": "# Project\n- goal: segment nodules\n- data: /data/ct",
    })

    assert await store.consolidate(session, provider, "m", archive_all=True)

    prompt = provider.chat.await_args.kwargs["messages"][1]["content"]
    assert "archived earlier: dataset at /data/ct" in prompt
    params = provider.chat.await_args.kwargs["tools"][0]["function"]["parameters"]["properties"]
    assert set(params) == {"history_entry", "project_memory_update"}
    assert store.read_long_term().endswith("- data: /data/ct")
    assert store.read_unprocessed_history(store.get_last_dream_cursor()) == []


async def test_plain_chat_consolidation_leaves_the_mind_to_dream(tmp_path: Path) -> None:
    store = MemoryStore(get_workspace_path(None), project=False)
    store.global_memory_file.write_text("# Long-term Memory\n- curated by Dream", encoding="utf-8")
    session = Session(key="k", messages=[{"role": "user", "content": "I love plots", "timestamp": "2026-01-01T11:00"}])
    provider = _save_memory_provider({
        "history_entry": "[2026-01-01 11:00] user loves plots",
        "project_memory_update": "should be ignored",
    })

    assert await store.consolidate(session, provider, "m", archive_all=True)

    params = provider.chat.await_args.kwargs["tools"][0]["function"]["parameters"]["properties"]
    assert set(params) == {"history_entry"}
    assert store.global_memory_file.read_text(encoding="utf-8") == "# Long-term Memory\n- curated by Dream"
    assert [r["content"] for r in store.read_unprocessed_history(0)] == ["[2026-01-01 11:00] user loves plots"]


async def test_agents_cannot_write_the_mind_but_dream_can(tmp_path: Path) -> None:
    mind = get_mind_dir()
    (mind / "USER.md").write_text("# User\n- Name: Ada", encoding="utf-8")

    out = await WriteFileTool(workspace=tmp_path).execute(path=str(mind / "MEMORY.md"), content="hacked")
    assert out.startswith("Error:") and "maintained by Dream" in out
    out = await EditFileTool(workspace=tmp_path).execute(path=str(mind / "USER.md"), old_text="Ada", new_text="Bob")
    assert out.startswith("Error:")
    assert not (mind / "MEMORY.md").exists()

    dream_edit = EditFileTool(workspace=mind, allowed_dir=mind, protect_mind=False)
    assert not (await dream_edit.execute(path="USER.md", old_text="Ada", new_text="Bob")).startswith("Error")
    assert (mind / "USER.md").read_text(encoding="utf-8").endswith("Bob")


def test_dream_refuses_a_project_store(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Dream(store=MemoryStore(tmp_path / "proj", project=True), provider=MagicMock(), model="m")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_dream_seeds_and_versions_the_mind() -> None:
    store = MemoryStore(get_workspace_path(None), project=False)
    store.append_history("user prefers short answers")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=MagicMock(content="[USER] prefers short answers", usage={}))
    dream = Dream(store=store, provider=provider, model="m")

    async def _edit(spec):
        (store.mind_dir / "USER.md").write_text("# User\n- prefers short answers\n", encoding="utf-8")
        return AgentRunResult(final_content="done", stop_reason="completed", messages=[], tools_used=[], usage={})

    dream._runner = MagicMock(run=_edit)
    assert await dream.run()

    assert (store.mind_dir / "SOUL.md").read_text(encoding="utf-8") == _builtin("SOUL.md")
    log = store.git.log()
    assert log[0].message.startswith("dream: ") and log[-1].message.startswith("init:")
    assert store.get_last_dream_cursor() == 1


def test_hand_written_history_rows_do_not_break_cursors(tmp_path: Path) -> None:
    store = MemoryStore(get_workspace_path(None), project=False)
    store.append_history("first")
    with open(store.history_jsonl_file, "a", encoding="utf-8") as f:
        f.write('{"cursor": "manual-note", "timestamp": "t", "content": "agent appended this"}\n')
        f.write("not json\n")

    assert store.append_history("second") == 2
    assert [r["content"] for r in store.read_unprocessed_history(0)] == ["first", "second"]
