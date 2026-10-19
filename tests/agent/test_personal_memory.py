"""Personal memory persists through Dream and is recalled across projects."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.memory import Dream, MemoryStore
from mira_engine.agent.tools.memory import EditMemoryTool
from mira_engine.bus.events import InboundMessage
from mira_engine.command.builtin import cmd_dream
from mira_engine.command.router import CommandContext
from mira_engine.providers.base import LLMResponse, ToolCallRequest


@pytest.fixture
def memory_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "home"))
    return tmp_path


async def test_dream_remembers_personal_facts_across_projects(memory_home):
    store = MemoryStore(memory_home / "project_a")
    store.append_history("User prefers Chinese replies. Project A uses PostgreSQL.")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content="[PERSONAL] Prefers Chinese replies\n[MEMORY] Uses PostgreSQL"),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="personal",
                        name="edit_memory",
                        arguments={
                            "scope": "personal",
                            "old_text": "",
                            "new_text": "- Prefers Chinese replies",
                        },
                    ),
                    ToolCallRequest(
                        id="project",
                        name="edit_memory",
                        arguments={
                            "scope": "project",
                            "old_text": "",
                            "new_text": "- Uses PostgreSQL",
                        },
                    ),
                ],
            ),
            LLMResponse(content="Memory updated."),
        ]
    )
    assert await Dream(store, provider, "test-model").run()

    # A new context and store simulate a later session in another project.
    prompt = ContextBuilder(memory_home / "project_b").build_system_prompt()
    assert "Prefers Chinese replies" in prompt
    assert "Uses PostgreSQL" not in prompt
    assert "Uses PostgreSQL" in ContextBuilder(store.workspace).build_system_prompt()
    assert store.read_memory() == "- Uses PostgreSQL"
    assert store._legacy_project_memory_file.read_text(encoding="utf-8") == "- Uses PostgreSQL"
    assert store.memory_backup_file.read_text(encoding="utf-8") == "- Uses PostgreSQL"
    assert store.get_last_dream_cursor() == 1


async def test_dream_includes_existing_personal_memory_in_both_phases(memory_home):
    store = MemoryStore(memory_home / "project")
    store.write_global_term("- Prefers Chinese replies")
    store.append_history("User now prefers English replies")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content="[PERSONAL] Prefers English replies"),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="correction",
                        name="edit_memory",
                        arguments={
                            "scope": "personal",
                            "old_text": "Chinese",
                            "new_text": "English",
                        },
                    )
                ],
            ),
            LLMResponse(content="Updated."),
        ]
    )
    assert await Dream(store, provider, "test-model").run()
    for call in provider.chat_with_retry.call_args_list[:2]:
        assert "Prefers Chinese replies" in call.kwargs["messages"][1]["content"]
    fresh = MemoryStore(memory_home / "another_project")
    assert fresh.read_global_term() == "- Prefers English replies"


async def test_personal_edits_preserve_other_projects_updates(memory_home):
    first = MemoryStore(memory_home / "first")
    second = MemoryStore(memory_home / "second")
    first.write_global_term("- Prefers Chinese\n- Uses Python")
    tool_a, tool_b = EditMemoryTool(first), EditMemoryTool(second)
    assert "Successfully" in await tool_a.execute("personal", "Chinese", "English")
    assert "Successfully" in await tool_b.execute("personal", "Python", "Rust")
    assert first.read_global_term() == "- Prefers English\n- Uses Rust"
    assert (await tool_a.execute("personal", "Chinese", "French")).startswith("Error")
    assert (await tool_a.execute("personal", "", "Overwrite")).startswith("Error")
    assert first.read_global_term() == "- Prefers English\n- Uses Rust"
    assert "Successfully" in await tool_a.execute("personal", "- Prefers English\n", "")
    assert second.read_global_term() == "- Uses Rust"


async def test_concurrent_change_is_not_overwritten(memory_home, monkeypatch):
    store = MemoryStore(memory_home / "project")
    store.write_global_term("Original")
    commit = store.commit_consolidated_memories

    def concurrent_commit(**kwargs):
        store.write_global_term("Concurrent update")
        return commit(**kwargs)

    monkeypatch.setattr(store, "commit_consolidated_memories", concurrent_commit)
    result = await EditMemoryTool(store).execute("personal", "Original", "Stale update")
    assert result.startswith("Error")
    assert store.read_global_term() == "Concurrent update"


async def test_failed_personal_edit_keeps_history_for_retry(memory_home):
    store = MemoryStore(memory_home / "project")
    store.write_global_term("Existing fact")
    store.append_history("A new preference")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content="[PERSONAL] A new preference"),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="stale",
                        name="edit_memory",
                        arguments={
                            "scope": "personal",
                            "old_text": "Missing text",
                            "new_text": "Preference",
                        },
                    )
                ],
            ),
        ]
    )
    assert not await Dream(store, provider, "test-model").run()
    assert store.get_last_dream_cursor() == 0
    assert len(store.read_unprocessed_history(since_cursor=0)) == 1
    assert store.read_global_term() == "Existing fact"


@pytest.mark.parametrize("scope", ["personal", "project"])
async def test_default_workspace_keeps_personal_and_project_memory_separate(memory_home, scope):
    store = MemoryStore(memory_home / "home" / "workspace")
    tool = EditMemoryTool(store)
    assert "Successfully" in await tool.execute(scope, "", "Fact")
    assert store.read_global_term() == ("Fact" if scope == "personal" else "")
    assert store.read_memory() == ("Fact" if scope == "project" else "")


async def test_memory_tool_rejects_ambiguous_matches_and_invalid_scopes(memory_home):
    store = MemoryStore(memory_home / "project")
    store.write_global_term("Fact\nFact")
    dream = Dream(store, MagicMock(), "test-model")
    for arguments in [
        {"scope": "personal", "old_text": "Fact", "new_text": "Changed"},
        {"scope": "../outside", "old_text": "Fact", "new_text": "Changed"},
    ]:
        result = await dream._tools.execute("edit_memory", arguments)
        assert result.startswith("Error")
    assert store.read_global_term() == "Fact\nFact"


@pytest.mark.parametrize("project_scoped", [True, False])
@pytest.mark.parametrize("provider_error", [True, False])
async def test_dream_command_processes_the_requesting_workspace(
    memory_home, project_scoped, provider_error
):
    global_store = MemoryStore(memory_home / "home" / "workspace")
    project_store = MemoryStore(memory_home / "project_a")
    global_store.append_history("GLOBAL WORKSPACE HISTORY")
    project_store.append_history("PROJECT A HISTORY: User prefers Chinese replies")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(content="[PERSONAL] Prefers Chinese replies"),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="remember",
                        name="edit_memory",
                        arguments={
                            "scope": "personal",
                            "old_text": "",
                            "new_text": "- Prefers Chinese replies",
                        },
                    )
                ],
            ),
            LLMResponse(content="Updated."),
        ]
    )
    if provider_error:
        provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="Invalid token", finish_reason="error")
        )
    loop = BaseAgentLoop.__new__(BaseAgentLoop)
    loop.dream = Dream(global_store, provider, "test-model")
    loop._project_contexts = {}
    completed = asyncio.Event()
    responses = []

    async def publish(message):
        responses.append(message)
        completed.set()

    loop.bus = SimpleNamespace(publish_outbound=publish)
    metadata = (
        {
            "project_id": "project_a",
            "project_dir": str(project_store.workspace),
        }
        if project_scoped
        else {}
    )
    message = InboundMessage(
        channel="ui",
        sender_id="tester",
        chat_id="demo-session",
        content="/dream",
        metadata=metadata,
    )
    context = CommandContext(
        msg=message, session=None, key=message.session_key, raw="/dream", loop=loop
    )
    initial = await cmd_dream(context)
    await asyncio.wait_for(completed.wait(), timeout=5)
    expected_history = "PROJECT A HISTORY" if project_scoped else "GLOBAL WORKSPACE HISTORY"
    other_history = "GLOBAL WORKSPACE HISTORY" if project_scoped else "PROJECT A HISTORY"
    history_prompt = provider.chat_with_retry.call_args_list[0].kwargs["messages"][1]["content"]
    assert expected_history in history_prompt
    assert other_history not in history_prompt
    assert initial.metadata == metadata
    assert responses[0].metadata == metadata
    if provider_error:
        assert "could not complete" in responses[0].content
        assert "history was kept" in responses[0].content
        assert project_store.get_last_dream_cursor() == 0
        assert global_store.get_last_dream_cursor() == 0
        provider.chat_with_retry.assert_awaited_once()
        return
    assert responses[0].content.startswith("Dream completed")
    assert project_store.get_last_dream_cursor() == (1 if project_scoped else 0)
    assert global_store.get_last_dream_cursor() == (0 if project_scoped else 1)
    assert (
        "Prefers Chinese replies" in ContextBuilder(memory_home / "project_b").build_system_prompt()
    )
