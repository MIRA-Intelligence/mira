"""Small durable counters, updated once per returned model call, never per token."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.bus.events import OutboundMessage


class TokenUsageStore:
    def __init__(self, directory: Path):
        self.path = directory / ".mira" / "token-usage.sqlite3"

    @staticmethod
    def _snapshot(db, session_id):
        row = db.execute(
            "SELECT COALESCE(SUM(input),0), COALESCE(SUM(output),0), COALESCE(SUM(total),0), "
            "COALESCE(SUM(cached),0), COALESCE(SUM(calls),0), COALESCE(SUM(missing),0) "
            "FROM usage WHERE session=?", (session_id,),
        ).fetchone()
        result = dict(zip(("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "calls", "unreported_calls"), row))
        # Older databases have no cache-coverage information. Never infer a
        # cache miss from their zero counter. A side table also lets an older
        # engine finish writing during an upgrade without changing its schema.
        has_coverage = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cache_reporting'").fetchone()
        result["cache_reported_calls"] = db.execute(
            "SELECT COALESCE(SUM(calls),0) FROM cache_reporting WHERE session=?", (session_id,),
        ).fetchone()[0] if has_coverage else 0
        return result

    def snapshot(self, session_id: str) -> dict[str, int]:
        if not self.path.exists():
            return dict.fromkeys(("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "calls", "unreported_calls", "cache_reported_calls"), 0)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("BEGIN")  # Totals and coverage must describe the same snapshot.
            return self._snapshot(db, session_id)

    def record(self, session_id: str, run_id: str, model: str, usage: dict) -> dict[str, int]:
        def count(key, fallback=None):
            value = usage.get(key, usage.get(fallback))
            return value if type(value) is int and value >= 0 else 0

        input_tokens = count("prompt_tokens", "input_tokens")
        output_tokens = count("completion_tokens", "output_tokens")
        total = max(count("total_tokens"), input_tokens + output_tokens)
        cached = count("cached_tokens") if count("cached_tokens") <= input_tokens else 0
        # A provider's absent/zero usage is not evidence that a call was free.
        missing = int(total == 0)
        cache_reported = int(
            type(usage.get("cached_tokens")) is int
            and 0 <= usage["cached_tokens"] <= input_tokens
            and not missing
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS usage (session TEXT, run TEXT, model TEXT, "
                       "input INTEGER, output INTEGER, total INTEGER, cached INTEGER, calls INTEGER, missing INTEGER, "
                       "PRIMARY KEY(session,run,model))")
            db.execute("CREATE TABLE IF NOT EXISTS cache_reporting (session TEXT, run TEXT, model TEXT, "
                       "calls INTEGER NOT NULL, PRIMARY KEY(session,run,model))")
            db.execute(
                "INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(session,run,model) DO UPDATE SET "
                "input=input+excluded.input, output=output+excluded.output, total=total+excluded.total, "
                "cached=cached+excluded.cached, calls=calls+1, missing=missing+excluded.missing",
                (session_id, run_id, model, input_tokens, output_tokens, total, cached, 1, missing),
            )
            db.execute(
                "INSERT INTO cache_reporting VALUES(?,?,?,?) ON CONFLICT(session,run,model) "
                "DO UPDATE SET calls=calls+excluded.calls",
                (session_id, run_id, model, cache_reported),
            )
            return self._snapshot(db, session_id)


@dataclass
class UsageContext:
    store: TokenUsageStore
    session_id: str
    run_id: str
    bus: Any
    metadata: dict


_current: ContextVar[UsageContext | None] = ContextVar("mira_token_usage", default=None)


def track_token_usage(method):
    """Task-local attribution inherited by delegated/background model calls."""
    @wraps(method)
    async def wrapped(loop, msg, *args, **kwargs):
        if _current.get() is not None or msg.channel != "ui":
            return await method(loop, msg, *args, **kwargs)
        metadata = dict(msg.metadata or {})
        project = loop._project_ref_from_metadata(metadata)
        directory = project.project_dir if project else loop.workspace
        token = _current.set(UsageContext(
            TokenUsageStore(directory), msg.chat_id, str(metadata.get("workflow_run_id") or ""), loop.bus, metadata,
        ))
        try:
            return await method(loop, msg, *args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped


async def record_token_usage(usage: object, model: str | None = None) -> None:
    context = _current.get()
    if context is None:
        return
    try:
        snapshot = await asyncio.to_thread(
            context.store.record, context.session_id, context.run_id, model or "unknown",
            usage if isinstance(usage, dict) else {},
        )
        await context.bus.publish_outbound(OutboundMessage(
            channel="ui", chat_id=context.session_id, content="",
            metadata={**context.metadata, "_usage_only": True, "token_usage": snapshot},
        ))
    except Exception as exc:
        # Accounting must not break an otherwise successful model response.
        logger.warning("Token accounting unavailable ({})", type(exc).__name__)
