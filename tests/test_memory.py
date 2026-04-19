from unittest.mock import AsyncMock, MagicMock

import pytest

from medpilot.agent.memory import MemoryStore
from medpilot.providers.base import LLMResponse, ToolCallRequest
from medpilot.session.manager import Session


def test_read_long_term_missing_and_present(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    assert store.read_long_term() == ""
    store.write_long_term("facts")
    assert store.read_long_term() == "facts"


def test_write_long_term_round_trip(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.write_long_term("alpha\nbeta")
    assert store.memory_file.read_text(encoding="utf-8") == "alpha\nbeta"


def test_append_history_format_and_accumulation(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.append_history("first")
    store.append_history("second\n")
    text = store.history_file.read_text(encoding="utf-8")
    assert text == "first\n\nsecond\n\n"


def test_get_memory_context_empty_and_nonempty(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    assert store.get_memory_context() == ""
    store.write_long_term("remember this")
    assert store.get_memory_context() == "## Long-term Memory\nremember this"


def test_align_boundary_to_user() -> None:
    msgs = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    assert MemoryStore._align_boundary_to_user(msgs, 2) == 2
    assert MemoryStore._align_boundary_to_user(msgs, 1) == 0
    assert MemoryStore._align_boundary_to_user(msgs, 0) == 0


@pytest.mark.asyncio
async def test_consolidate_archive_all_updates_memory_and_resets_marker(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    session = Session(
        key="t:1",
        messages=[
            {"role": "user", "content": "hi", "timestamp": "2025-01-01T12:00:00"},
        ],
        last_consolidated=2,
    )
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="1",
                    name="save_memory",
                    arguments={
                        "history_entry": "[2025-01-01 12:00] summary",
                        "memory_update": "new memory",
                    },
                )
            ],
        )
    )
    ok = await store.consolidate(session, provider, "m", archive_all=True)
    assert ok is True
    assert session.last_consolidated == 0
    assert "new memory" == store.read_long_term()
    assert "[2025-01-01 12:00] summary" in store.history_file.read_text(encoding="utf-8")
    provider.chat.assert_awaited_once()
    kwargs = provider.chat.await_args.kwargs
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "save_memory"}}


@pytest.mark.asyncio
async def test_consolidate_normal_path_advances_last_consolidated(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    messages = []
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"m{i}", "timestamp": "2025-01-01T12:00:00"})
    session = Session(key="t:1", messages=messages, last_consolidated=0)
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="1",
                    name="save_memory",
                    arguments={
                        "history_entry": "entry",
                        "memory_update": "mem",
                    },
                )
            ],
        )
    )
    ok = await store.consolidate(session, provider, "m", memory_window=50)
    assert ok is True
    assert session.last_consolidated == 4
    provider.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_consolidate_no_tool_calls_returns_false(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    messages = [{"role": "user", "content": "x", "timestamp": "2025-01-01T12:00:00"}] * 30
    session = Session(key="t:1", messages=messages, last_consolidated=0)
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LLMResponse(content="nope", tool_calls=[]))
    ok = await store.consolidate(session, provider, "m", memory_window=50)
    assert ok is False
    assert provider.chat.await_count == 3


@pytest.mark.asyncio
async def test_consolidate_retries_and_then_succeeds(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    messages = [{"role": "user", "content": "x", "timestamp": "2025-01-01T12:00:00"}] * 30
    session = Session(key="t:1", messages=messages, last_consolidated=0)
    provider = MagicMock()
    provider.chat = AsyncMock(
        side_effect=[
            LLMResponse(content="first attempt without tool", tool_calls=[]),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="1",
                        name="save_memory",
                        arguments={
                            "history_entry": "entry",
                            "project_memory_update": "project memory",
                        },
                    )
                ],
            ),
        ]
    )

    ok = await store.consolidate(session, provider, "m", memory_window=50)
    assert ok is True
    assert provider.chat.await_count == 2
    assert store.read_long_term() == "project memory"


@pytest.mark.asyncio
async def test_consolidate_json_text_fallback_without_tool_call(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    messages = [{"role": "user", "content": "x", "timestamp": "2025-01-01T12:00:00"}] * 30
    session = Session(key="t:1", messages=messages, last_consolidated=0)
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content='{"history_entry":"entry","project_memory_update":"from json"}',
            tool_calls=[],
        )
    )

    ok = await store.consolidate(session, provider, "m", memory_window=50)
    assert ok is True
    assert provider.chat.await_count == 1
    assert store.read_long_term() == "from json"


@pytest.mark.asyncio
async def test_consolidate_exception_returns_false(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    session = Session(
        key="t:1",
        messages=[{"role": "user", "content": "x", "timestamp": "2025-01-01T12:00:00"}],
        last_consolidated=0,
    )
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("boom"))
    ok = await store.consolidate(session, provider, "m", archive_all=True)
    assert ok is False
