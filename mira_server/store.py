"""SQLite store: users, sessions and hashed tokens, engine records and the audit log.

The database lives in ``data_dir`` (mode 0700) as ``mira-server.sqlite3``
(mode 0600). It never holds passwords or raw tokens; tokens are stored as
SHA-256 hashes. The audit log records metadata only (event, user, client IP,
small structured details), never request bodies, prompts or tokens.

All access happens on the event loop thread; each statement is short.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    uid INTEGER,
    display_name TEXT,
    roles TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    client TEXT,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(username);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE INDEX IF NOT EXISTS tokens_by_session ON tokens(session_id);
CREATE TABLE IF NOT EXISTS engines (
    username TEXT PRIMARY KEY,
    port INTEGER,
    pid INTEGER,
    version TEXT,
    started_by_server INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    username TEXT,
    client_ip TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_ts ON audit(ts);
"""


@dataclass(frozen=True)
class TokenRow:
    token_hash: str
    session_id: str
    kind: str
    issued_at: float
    expires_at: float
    used_at: float | None
    username: str
    session_revoked_at: float | None


@dataclass(frozen=True)
class EngineRow:
    username: str
    port: int | None
    pid: int | None
    version: str | None
    started_by_server: bool
    updated_at: float


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` with mode 0700, or tighten an existing directory we own."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_mode & 0o077:
        if info.st_uid == os.geteuid():
            path.chmod(0o700)
        else:
            log.warning("data dir %s is accessible to other users (mode %o)", path, info.st_mode & 0o777)


def _ensure_private_file(path: Path) -> None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)


class Store:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        if str(path) != ":memory:":
            ensure_private_dir(self.path.parent)
            _ensure_private_file(self.path)
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self._db.close()

    def _now(self) -> float:
        return self._clock()

    # ── users ────────────────────────────────────────────────────────

    def upsert_user(self, username: str, *, uid: int | None, display_name: str, roles: list[str],
                    login: bool = False) -> None:
        now = self._now()
        self._db.execute(
            """INSERT INTO users(username, uid, display_name, roles, created_at, last_login_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET uid = excluded.uid,
                   display_name = excluded.display_name, roles = excluded.roles,
                   last_login_at = COALESCE(excluded.last_login_at, users.last_login_at)""",
            (username, uid, display_name, json.dumps(roles), now, now if login else None),
        )

    def get_user(self, username: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["roles"] = json.loads(data["roles"] or "[]")
        return data

    # ── sessions and tokens ──────────────────────────────────────────

    def create_session(self, session_id: str, username: str, client: str | None) -> None:
        self._db.execute(
            "INSERT INTO sessions(id, username, client, created_at, last_used_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, username, client, self._now(), self._now()),
        )

    def add_token(self, token_hash: str, session_id: str, kind: str, expires_at: float) -> None:
        self._db.execute(
            "INSERT INTO tokens(token_hash, session_id, kind, issued_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, session_id, kind, self._now(), expires_at),
        )

    def get_token(self, token_hash: str) -> TokenRow | None:
        row = self._db.execute(
            """SELECT t.token_hash, t.session_id, t.kind, t.issued_at, t.expires_at, t.used_at,
                      s.username, s.revoked_at AS session_revoked_at
               FROM tokens t JOIN sessions s ON s.id = t.session_id
               WHERE t.token_hash = ?""",
            (token_hash,),
        ).fetchone()
        return TokenRow(**dict(row)) if row is not None else None

    def mark_token_used(self, token_hash: str) -> bool:
        """Mark a refresh token consumed; False when it already was (a reuse)."""
        cur = self._db.execute("UPDATE tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                               (self._now(), token_hash))
        return cur.rowcount == 1

    def touch_session(self, session_id: str) -> None:
        self._db.execute("UPDATE sessions SET last_used_at = ? WHERE id = ?", (self._now(), session_id))

    def revoke_session(self, session_id: str, reason: str) -> None:
        self._db.execute(
            "UPDATE sessions SET revoked_at = ?, revoke_reason = ? WHERE id = ? AND revoked_at IS NULL",
            (self._now(), reason, session_id),
        )

    def revoke_user_sessions(self, username: str, reason: str) -> int:
        cur = self._db.execute(
            "UPDATE sessions SET revoked_at = ?, revoke_reason = ? WHERE username = ? AND revoked_at IS NULL",
            (self._now(), reason, username),
        )
        return cur.rowcount

    def active_session_users(self) -> list[str]:
        rows = self._db.execute("SELECT DISTINCT username FROM sessions WHERE revoked_at IS NULL").fetchall()
        return [row["username"] for row in rows]

    def purge_expired(self, *, keep_revoked_s: float = 7 * 86_400) -> None:
        """Drop expired tokens and sessions that are long revoked or have no live tokens."""
        now = self._now()
        self._db.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
        self._db.execute("DELETE FROM sessions WHERE revoked_at IS NOT NULL AND revoked_at < ?",
                         (now - keep_revoked_s,))
        self._db.execute(
            """DELETE FROM sessions WHERE revoked_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM tokens t WHERE t.session_id = sessions.id)""")

    # ── engines ──────────────────────────────────────────────────────

    def put_engine(self, username: str, *, port: int | None, pid: int | None, version: str | None,
                   started_by_server: bool) -> None:
        self._db.execute(
            """INSERT INTO engines(username, port, pid, version, started_by_server, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET port = excluded.port, pid = excluded.pid,
                   version = excluded.version, started_by_server = excluded.started_by_server,
                   updated_at = excluded.updated_at""",
            (username, port, pid, version, int(started_by_server), self._now()),
        )

    def get_engine(self, username: str) -> EngineRow | None:
        row = self._db.execute("SELECT * FROM engines WHERE username = ?", (username,)).fetchone()
        return self._engine_row(row) if row is not None else None

    def list_engines(self) -> list[EngineRow]:
        return [self._engine_row(row) for row in self._db.execute("SELECT * FROM engines").fetchall()]

    def delete_engine(self, username: str) -> None:
        self._db.execute("DELETE FROM engines WHERE username = ?", (username,))

    @staticmethod
    def _engine_row(row: sqlite3.Row) -> EngineRow:
        data = dict(row)
        data["started_by_server"] = bool(data["started_by_server"])
        return EngineRow(**data)

    # ── audit ────────────────────────────────────────────────────────

    def audit(self, event: str, *, username: str | None = None, client_ip: str | None = None,
              **detail: Any) -> None:
        payload = json.dumps(detail, default=str, sort_keys=True) if detail else None
        self._db.execute("INSERT INTO audit(ts, event, username, client_ip, detail) VALUES (?, ?, ?, ?, ?)",
                         (self._now(), event, username, client_ip, payload))

    def audit_events(self, event: str | None = None) -> list[dict[str, Any]]:
        if event is None:
            rows = self._db.execute("SELECT * FROM audit ORDER BY id").fetchall()
        else:
            rows = self._db.execute("SELECT * FROM audit WHERE event = ? ORDER BY id", (event,)).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["detail"] = json.loads(data["detail"]) if data["detail"] else {}
            result.append(data)
        return result
