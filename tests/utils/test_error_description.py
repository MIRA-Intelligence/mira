"""Tests for user-facing error description helpers."""

import asyncio

from mira_engine.utils.runtime import (
    DISPATCH_ERROR_PREFIX,
    build_dispatch_error_text,
    classify_exception,
    describe_exception,
    error_detail_text,
)


def test_classify_exception_codes():
    assert classify_exception(asyncio.TimeoutError()) == "timeout"
    assert classify_exception(ConnectionError("connection refused")) == "network"
    assert classify_exception(RuntimeError("401 Unauthorized")) == "auth"
    assert classify_exception(RuntimeError("429 rate limit")) == "rate_limit"
    assert classify_exception(ValueError("maximum context length")) == "context_window"
    assert classify_exception(KeyError("widget")) == "unknown"


def test_error_detail_text():
    assert error_detail_text(RuntimeError("boom")) == "RuntimeError: boom"
    long = error_detail_text(RuntimeError("y" * 1000))
    assert long.startswith("RuntimeError:")
    assert long.endswith("…")


def test_describe_timeout():
    assert "timed out" in describe_exception(asyncio.TimeoutError())
    assert "timed out" in describe_exception(TimeoutError("read timed out"))


def test_describe_connection():
    assert "network error" in describe_exception(ConnectionError("connection refused"))
    assert "network error" in describe_exception(RuntimeError("DNS name resolution failed"))


def test_describe_auth():
    reason = describe_exception(RuntimeError("401 Unauthorized: invalid api key"))
    assert "authentication failed" in reason


def test_describe_rate_limit():
    reason = describe_exception(RuntimeError("429 Too Many Requests"))
    assert "rate-limited" in reason


def test_describe_context_window():
    reason = describe_exception(ValueError("maximum context length exceeded"))
    assert "context window" in reason


def test_describe_fallback_includes_type_and_message():
    reason = describe_exception(KeyError("widget"))
    assert reason.startswith("KeyError:")
    assert "widget" in reason


def test_describe_truncates_long_detail():
    reason = describe_exception(RuntimeError("x" * 1000))
    assert reason.endswith("…")
    assert len(reason) < 400


def test_build_dispatch_error_text_cli_hint():
    text = build_dispatch_error_text(RuntimeError("boom"), channel="cli")
    assert text.startswith(DISPATCH_ERROR_PREFIX)
    assert "RuntimeError: boom" in text
    assert "mira agent --logs" in text


def test_build_dispatch_error_text_non_cli_has_no_logs_hint():
    text = build_dispatch_error_text(RuntimeError("boom"), channel="ui")
    assert "mira agent --logs" not in text
    assert "RuntimeError: boom" in text
