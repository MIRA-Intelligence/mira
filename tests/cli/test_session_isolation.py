"""Tests for per-instance CLI session isolation.

Regression coverage for the multi-instance contamination bug: two concurrently
running ``mira`` CLI processes must default to *distinct* session ids so their
conversations never interleave into the same session transcript. An explicit
``--session`` must still be honoured verbatim so users can resume a session.
"""

from __future__ import annotations

from mira_engine.cli.commands import _resolve_cli_session_id
from mira_engine.session.manager import SessionManager


def test_explicit_session_id_is_honoured() -> None:
    assert _resolve_cli_session_id("cli:direct", prefix="cli") == "cli:direct"
    assert _resolve_cli_session_id("my-session", prefix="research") == "my-session"


def test_explicit_session_id_is_trimmed() -> None:
    assert _resolve_cli_session_id("  keep-me  ", prefix="cli") == "keep-me"


def test_none_generates_prefixed_unique_id() -> None:
    generated = _resolve_cli_session_id(None, prefix="cli")
    assert generated.startswith("cli:")
    # The suffix must be a non-empty unique token (not the legacy "direct").
    suffix = generated.split(":", 1)[1]
    assert suffix and suffix != "direct"


def test_blank_session_id_is_treated_as_unset() -> None:
    generated = _resolve_cli_session_id("   ", prefix="research")
    assert generated.startswith("research:")
    assert generated != "research:"


def test_concurrent_instances_get_distinct_sessions() -> None:
    ids = {_resolve_cli_session_id(None, prefix="cli") for _ in range(200)}
    # Every "instance" must get its own session id -> no cross-contamination.
    assert len(ids) == 200


def test_generated_session_id_splits_into_channel_and_chat() -> None:
    # Interactive mode splits the session id on the first ':' into
    # channel:chat_id, so the generated id must contain exactly that separator.
    generated = _resolve_cli_session_id(None, prefix="cli")
    channel, _, chat_id = generated.partition(":")
    assert channel == "cli"
    assert chat_id


def test_default_cli_instances_persist_to_different_transcripts(tmp_path) -> None:
    first_id = _resolve_cli_session_id(None, prefix="cli")
    second_id = _resolve_cli_session_id(None, prefix="cli")
    manager = SessionManager(tmp_path)

    first = manager.get_or_create(first_id)
    first.add_message("user", "first instance only")
    manager.save(first)
    second = manager.get_or_create(second_id)
    second.add_message("user", "second instance only")
    manager.save(second)

    first_text = manager._get_session_path(first_id).read_text(encoding="utf-8")
    second_text = manager._get_session_path(second_id).read_text(encoding="utf-8")
    assert "first instance only" in first_text
    assert "second instance only" not in first_text
    assert "second instance only" in second_text
    assert "first instance only" not in second_text
