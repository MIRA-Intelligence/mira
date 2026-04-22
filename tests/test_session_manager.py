"""Tests for SessionManager – save/load round-trip, list, cache, legacy migration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from medpilot.session.manager import Session, SessionManager


@pytest.fixture
def manager(tmp_path: Path) -> SessionManager:
    with patch("medpilot.session.manager.get_legacy_sessions_dir", return_value=tmp_path / "legacy"):
        return SessionManager(tmp_path)


# ── get_or_create / cache ──────────────────────────────────────────

def test_get_or_create_returns_new_session(manager: SessionManager) -> None:
    s = manager.get_or_create("cli:test1")
    assert s.key == "cli:test1"
    assert s.messages == []


def test_get_or_create_returns_same_from_cache(manager: SessionManager) -> None:
    s1 = manager.get_or_create("cli:test1")
    s2 = manager.get_or_create("cli:test1")
    assert s1 is s2


def test_invalidate_removes_from_cache(manager: SessionManager) -> None:
    s1 = manager.get_or_create("cli:test1")
    manager.invalidate("cli:test1")
    s2 = manager.get_or_create("cli:test1")
    assert s1 is not s2


# ── save / load round-trip ─────────────────────────────────────────

def test_save_and_reload(manager: SessionManager) -> None:
    s = manager.get_or_create("cli:roundtrip")
    s.add_message("user", "hello")
    s.add_message("assistant", "hi there")
    s.last_consolidated = 1
    s.metadata = {"foo": "bar"}
    manager.save(s)

    manager.invalidate("cli:roundtrip")
    loaded = manager.get_or_create("cli:roundtrip")

    assert loaded.key == "cli:roundtrip"
    assert len(loaded.messages) == 2
    assert loaded.messages[0]["role"] == "user"
    assert loaded.messages[0]["content"] == "hello"
    assert loaded.messages[1]["role"] == "assistant"
    assert loaded.messages[1]["content"] == "hi there"
    assert loaded.last_consolidated == 1
    assert loaded.metadata == {"foo": "bar"}


def test_save_preserves_tool_calls(manager: SessionManager) -> None:
    s = manager.get_or_create("cli:tools")
    s.messages = [
        {"role": "user", "content": "run check"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_1", "type": "function", "function": {"name": "check", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_1", "name": "check", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    manager.save(s)
    manager.invalidate("cli:tools")

    loaded = manager.get_or_create("cli:tools")
    assert loaded.messages[1]["tool_calls"][0]["id"] == "tc_1"
    assert loaded.messages[2]["tool_call_id"] == "tc_1"


def test_save_appends_events_without_rewriting(manager: SessionManager) -> None:
    s = manager.get_or_create("cli:append")
    s.add_message("user", "first")
    manager.save(s)
    path = manager._get_session_path("cli:append")
    first_save_lines = len(path.read_text(encoding="utf-8").splitlines())

    s.add_message("assistant", "second")
    manager.save(s)
    second_save_lines = len(path.read_text(encoding="utf-8").splitlines())

    assert second_save_lines > first_save_lines

    manager.invalidate("cli:append")
    loaded = manager.get_or_create("cli:append")
    assert [m["content"] for m in loaded.messages] == ["first", "second"]


def test_append_ui_event_round_trip(manager: SessionManager) -> None:
    manager.append_ui_event(
        key="web:PRJ-0001",
        role="user",
        content="hello ui",
        msg_type="response",
        metadata={"_user": True},
        timestamp="2026-03-24T12:00:00",
    )
    manager.append_ui_event(
        key="web:PRJ-0001",
        role="assistant",
        content="hello back",
        msg_type="response",
        metadata={},
        timestamp="2026-03-24T12:00:01",
    )

    entries = manager.get_ui_history("web:PRJ-0001")
    assert entries[0]["content"] == "hello ui"
    assert entries[0]["metadata"]["_user"] is True
    assert entries[1]["content"] == "hello back"

def test_load_corrupt_file_returns_new_session(manager: SessionManager) -> None:
    path = manager._get_session_path("cli:corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all\n", encoding="utf-8")

    s = manager.get_or_create("cli:corrupt")
    assert s.messages == []


def test_load_empty_file_returns_new_session(manager: SessionManager) -> None:
    path = manager._get_session_path("cli:empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    s = manager.get_or_create("cli:empty")
    assert s.messages == []


# ── legacy migration ───────────────────────────────────────────────

def test_legacy_session_migrated(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()

    with patch("medpilot.session.manager.get_legacy_sessions_dir", return_value=legacy_dir):
        mgr = SessionManager(tmp_path)

    safe_key = "cli_migrate"
    legacy_path = legacy_dir / f"{safe_key}.jsonl"
    metadata = {"_type": "metadata", "key": "cli:migrate", "created_at": "2025-01-01T00:00:00", "updated_at": "2025-01-01T00:00:00", "metadata": {}, "last_consolidated": 0}
    msg = {"role": "user", "content": "old message"}
    legacy_path.write_text(json.dumps(metadata) + "\n" + json.dumps(msg) + "\n", encoding="utf-8")

    s = mgr.get_or_create("cli:migrate")
    assert len(s.messages) == 1
    assert s.messages[0]["content"] == "old message"
    assert not legacy_path.exists()


# ── list_sessions ──────────────────────────────────────────────────

def test_list_sessions_returns_saved(manager: SessionManager) -> None:
    s1 = manager.get_or_create("cli:a")
    s1.add_message("user", "a")
    manager.save(s1)

    s2 = manager.get_or_create("cli:b")
    s2.add_message("user", "b")
    manager.save(s2)

    sessions = manager.list_sessions()
    keys = [s["key"] for s in sessions]
    assert "cli:a" in keys
    assert "cli:b" in keys
    assert len(sessions) >= 2


def test_list_sessions_sorted_by_updated_at(manager: SessionManager) -> None:
    import time

    s1 = manager.get_or_create("cli:first")
    s1.add_message("user", "first")
    manager.save(s1)

    time.sleep(0.01)

    s2 = manager.get_or_create("cli:second")
    s2.add_message("user", "second")
    manager.save(s2)

    sessions = manager.list_sessions()
    assert sessions[0]["key"] == "cli:second"
    assert sessions[1]["key"] == "cli:first"


def test_list_sessions_skips_non_metadata_file(manager: SessionManager) -> None:
    bad_file = manager.sessions_dir / "garbage.jsonl"
    bad_file.write_text('{"role": "user", "content": "not metadata"}\n', encoding="utf-8")

    sessions = manager.list_sessions()
    keys = [s["key"] for s in sessions]
    assert "garbage" not in keys


# ── Session.add_message ────────────────────────────────────────────

def test_add_message_appends_and_updates_timestamp() -> None:
    s = Session(key="cli:test")
    s.add_message("user", "hello")
    assert len(s.messages) == 1
    assert s.messages[0]["role"] == "user"
    assert "timestamp" in s.messages[0]


def test_add_message_with_kwargs() -> None:
    s = Session(key="cli:test")
    s.add_message("assistant", "done", tool_calls=[{"id": "tc_1"}])
    assert s.messages[0]["tool_calls"] == [{"id": "tc_1"}]


# ── Session.clear ──────────────────────────────────────────────────

def test_clear_resets_session() -> None:
    s = Session(key="cli:test", messages=[{"role": "user", "content": "hi"}], last_consolidated=5)
    s.clear()
    assert s.messages == []
    assert s.last_consolidated == 0
