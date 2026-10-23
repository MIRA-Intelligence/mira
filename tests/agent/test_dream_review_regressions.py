"""Regressions for policy refusals, bootstrap profiles and bounded Dream retries."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.memory import Dream, DreamRunError, MemoryStore
from mira_engine.agent.runner import AgentRunResult
from mira_engine.agent.tools.memory import EditMemoryTool
from mira_engine.providers.base import LLMResponse, ToolCallRequest


def tool_response(name, **arguments):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id="call", name=name, arguments=arguments,
    )])


def provider_for(*responses):
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[LLMResponse(content="Analysis"), *responses])
    return provider


def run_result(reason):
    return AgentRunResult(final_content="Done", stop_reason=reason,
                          messages=[], tools_used=[], usage={}, tool_events=[])


async def test_rejected_user_file_edit_can_be_repaired_with_scoped_edit(tmp_path):
    store = MemoryStore(tmp_path / "project")
    user = store.global_workspace / "USER.md"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text("### Communication Style\n- Verbose")
    store.append_history("User prefers concise replies")
    provider = provider_for(
        tool_response("edit_file", path="USER.md", old_text="Verbose", new_text="Concise"),
        tool_response("edit_memory", scope="user", old_text="Verbose", new_text="Concise"),
        LLMResponse(content="Done"),
    )
    assert await Dream(store, provider, "test").run()
    assert "Concise" in user.read_text()
    assert store.get_last_dream_cursor() == 1


@pytest.mark.parametrize("name,arguments", [
    ("edit_file", {"path": "USER.local.md", "old_text": "English", "new_text": "French"}),
    ("edit_file", {"path": "../SOUL.md", "old_text": "Helpful", "new_text": "Rude"}),
    ("read_file", {"path": "memory/MEMORY.md"}),
])
async def test_policy_refusals_and_read_errors_do_not_block_completed_runs(tmp_path, name, arguments):
    store = MemoryStore(tmp_path / "project")
    settings = store.workspace / "USER.local.md"
    settings.write_text("English")
    store.append_history("Respect configured settings")
    provider = provider_for(tool_response(name, **arguments), LLMResponse(content="No change needed"))
    dream = Dream(store, provider, "test")
    assert await dream.run()
    assert store.get_last_dream_cursor() == 1
    for _ in range(2):
        assert await Dream(store, provider, "test").run() is False
    assert settings.read_text() == "English"
    assert provider.chat_with_retry.await_count == 3


async def test_soul_write_failure_and_repair_use_the_same_canonical_target(tmp_path):
    store = MemoryStore(tmp_path / "project")
    store.write_soul("Helpful")
    store.append_history("Be practical")
    provider = provider_for(
        tool_response("edit_file", path="SOUL.md", old_text="Missing", new_text="Practical"),
        tool_response("edit_file", path=str(store.soul_file), old_text="Helpful", new_text="Practical"),
        LLMResponse(content="Done"),
    )
    assert await Dream(store, provider, "test").run()
    assert store.read_soul() == "Practical"
    assert store.get_last_dream_cursor() == 1


async def test_first_user_write_preserves_the_runtime_profile_template(tmp_path):
    store = MemoryStore(tmp_path / "project")
    template = ContextBuilder._load_builtin_template("USER.md")
    assert "Response Length" in template
    sections = [line for line in template.splitlines() if line.startswith("#")]
    result = await EditMemoryTool(store).execute(
        "user", "- General research assistance", "- Robotics",
    )
    assert result.startswith("Successfully")
    prompt = ContextBuilder(store.workspace).build_system_prompt()
    assert "Robotics" in prompt
    assert all(section in prompt for section in sections)
    assert "Response Length" in prompt
    assert prompt.count("### Communication Style") == 1


async def test_batch_retry_is_deferred_and_each_tick_has_one_phase_two_attempt(tmp_path):
    store = MemoryStore(tmp_path / "project")
    for i in range(20):
        store.append_history(f"Event {i}")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Analysis"))
    for expected_size in (20, 10, 1):
        dream = Dream(store, provider, "test")
        dream._runner.run = AsyncMock(return_value=run_result("max_iterations"))
        with pytest.raises(DreamRunError):
            await dream.run()
        dream._runner.run.assert_awaited_once()
        context = provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]
        assert f"Event {expected_size - 1}\n" in context
        assert f"Event {expected_size}\n" not in context
    assert provider.chat_with_retry.await_count == 3
    assert store.get_last_dream_cursor() == 1  # Only the failing entry is skipped.
    skipped = [json.loads(line) for line in (store.memory_dir / "dream_skipped.jsonl").read_text().splitlines()]
    assert len(skipped) == 1
    assert skipped[0]["entry"]["content"] == "Event 0"
    assert skipped[0]["attempts"] == 3
    assert len(store.read_unprocessed_history(1)) == 19
    dream = Dream(store, provider, "test")
    dream._runner.run = AsyncMock(return_value=run_result("completed"))
    assert await dream.run()
    assert store.get_last_dream_cursor() == 20


async def test_unresolved_write_is_bounded_across_new_dream_instances(tmp_path):
    store = MemoryStore(tmp_path / "project")
    store.write_global_term("Original")
    store.append_history("Unrepairable fact")
    for _ in range(3):
        provider = provider_for(
            tool_response("edit_memory", scope="personal", old_text="Missing", new_text="Changed"),
            LLMResponse(content="Done"),
        )
        with pytest.raises(DreamRunError):
            await Dream(store, provider, "test").run()
    assert store.get_last_dream_cursor() == 1
    assert store.read_global_term() == "Original"
    assert "Unrepairable fact" in (store.memory_dir / "dream_skipped.jsonl").read_text()
    store.append_history("Healthy next entry")
    assert await Dream(store, provider_for(LLMResponse(content="No changes")), "test").run()
    assert store.get_last_dream_cursor() == 2


async def test_provider_outage_never_discards_pending_history(tmp_path):
    store = MemoryStore(tmp_path / "project")
    store.append_history("Keep this entry")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Bad token", finish_reason="error"))
    for _ in range(4):
        with pytest.raises(DreamRunError):
            await Dream(store, provider, "test").run()
    assert store.get_last_dream_cursor() == 0
    assert not (store.memory_dir / "dream_skipped.jsonl").exists()


async def test_soul_symlink_outside_workspace_is_a_policy_refusal(tmp_path):
    store = MemoryStore(tmp_path / "project")
    outside = tmp_path / "private-soul.md"
    outside.write_text("Private")
    store.soul_file.symlink_to(outside)
    store.append_history("Keep filesystem boundaries")
    provider = provider_for(
        tool_response("edit_file", path="SOUL.md", old_text="Private", new_text="Changed"),
        LLMResponse(content="Read-only; leave unchanged"),
    )
    assert await Dream(store, provider, "test").run()
    assert outside.read_text() == "Private"


async def test_effective_user_template_is_present_in_both_dream_phases(tmp_path):
    store = MemoryStore(tmp_path / "project")
    store.append_history("Interests are already configured")
    provider = provider_for(LLMResponse(content="No changes"))
    assert await Dream(store, provider, "test").run()
    template = ContextBuilder._load_builtin_template("USER.md")
    for call in provider.chat_with_retry.call_args_list:
        prompt = call.kwargs["messages"][1]["content"]
        assert template.strip() in prompt
        assert "### Communication Style" in prompt
        assert "### Response Length" in prompt
    assert not (store.global_workspace / "USER.md").exists()


@pytest.mark.parametrize("scope", ["Personal", "invalid"])
@pytest.mark.parametrize("repair", [False, True])
async def test_invalid_memory_scope_requires_a_repaired_write(tmp_path, scope, repair):
    store = MemoryStore(tmp_path / "project")
    store.append_history("Remember reusable workflow A")
    responses = [tool_response("edit_memory", scope=scope, old_text="", new_text="Workflow A")]
    if repair:
        responses.append(tool_response("edit_memory", scope="personal", old_text="", new_text="Workflow A"))
    provider = provider_for(*responses, LLMResponse(content="Done"))
    if repair:
        assert await Dream(store, provider, "test").run()
    else:
        with pytest.raises(DreamRunError, match="Unresolved"):
            await Dream(store, provider, "test").run()
    assert store.get_last_dream_cursor() == int(repair)
    assert store.read_global_term() == ("Workflow A" if repair else "")


async def test_overlapping_dreams_do_not_duplicate_work_or_lose_retry_counts(tmp_path):
    store = MemoryStore(tmp_path / "project")
    store.append_history("Pending entry")
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Analysis"))
    started, release = asyncio.Event(), asyncio.Event()

    async def fail_after_release(spec):
        started.set()
        await release.wait()
        return run_result("max_iterations")

    first, second = (Dream(store, provider, "test") for _ in range(2))
    for dream in (first, second):
        dream._runner.run = AsyncMock(side_effect=fail_after_release)
    task = asyncio.create_task(first.run())
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        with pytest.raises(DreamRunError, match="already running"):
            await asyncio.wait_for(second.run(), timeout=0.5)
    finally:
        release.set()
        with pytest.raises(DreamRunError, match="max_iterations"):
            await task
    second._runner.run.assert_not_awaited()
    state = store.memory_dir / ".dream_retry.json"
    assert json.loads(state.read_text())["attempts"] == 1
    with pytest.raises(DreamRunError, match="max_iterations"):
        await second.run()
    assert json.loads(state.read_text())["attempts"] == 2
    assert provider.chat_with_retry.await_count == 2


async def test_empty_final_responses_are_bounded_and_later_history_proceeds(tmp_path):
    store = MemoryStore(tmp_path / "project")
    store.append_history("Entry with repeated empty model output")
    for attempt in range(1, 4):
        # Two empty turns plus the runner's finalization attempt must stay empty.
        provider = provider_for(*(LLMResponse(content="") for _ in range(3)))
        with pytest.raises(DreamRunError, match="empty_final_response|Skipped"):
            await Dream(store, provider, "test").run()
        if attempt < 3:
            assert store.get_last_dream_cursor() == 0
            state = store.memory_dir / ".dream_retry.json"
            assert json.loads(state.read_text())["attempts"] == attempt
    assert store.get_last_dream_cursor() == 1
    skipped = store.memory_dir / "dream_skipped.jsonl"
    record = json.loads(skipped.read_text())
    assert record["attempts"] == 3
    assert record["reason"] == "empty_final_response"
    assert record["entry"]["content"] == "Entry with repeated empty model output"
    store.append_history("Healthy next entry")
    assert await Dream(store, provider_for(LLMResponse(content="No changes")), "test").run()
    assert store.get_last_dream_cursor() == 2
