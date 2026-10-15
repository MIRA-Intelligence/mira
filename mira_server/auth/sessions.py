"""Access and refresh tokens.

A login creates a *session* (the refresh family). Each session holds short-lived
access tokens and one current refresh token. Refreshing consumes the refresh
token and issues a new pair in the same session; presenting a consumed refresh
token again means it was copied, so the whole session is revoked.

Only SHA-256 hashes of tokens are stored.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from mira_server.store import Store

ACCESS_PREFIX = "msa_"
REFRESH_PREFIX = "msr_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int

    def as_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "refresh_expires_in": self.refresh_expires_in,
        }


@dataclass(frozen=True)
class AccessGrant:
    session_id: str
    username: str


class RefreshError(Exception):
    """``reason`` is one of invalid, expired, revoked, reused."""

    def __init__(self, reason: str, *, username: str | None = None, session_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.username = username
        self.session_id = session_id


class SessionService:
    def __init__(self, store: Store, *, access_ttl: int, refresh_ttl: int,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self._clock = clock

    def _issue_pair(self, session_id: str) -> TokenPair:
        now = self._clock()
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        self._store.add_token(hash_token(access), session_id, "access", now + self.access_ttl)
        self._store.add_token(hash_token(refresh), session_id, "refresh", now + self.refresh_ttl)
        return TokenPair(access, self.access_ttl, refresh, self.refresh_ttl)

    def login(self, username: str, client: str | None) -> tuple[str, TokenPair]:
        session_id = uuid.uuid4().hex
        self._store.create_session(session_id, username, client)
        return session_id, self._issue_pair(session_id)

    def check_access(self, token: str | None) -> AccessGrant | None:
        if not token or not token.startswith(ACCESS_PREFIX):
            return None
        row = self._store.get_token(hash_token(token))
        if row is None or row.kind != "access" or row.session_revoked_at is not None:
            return None
        if row.expires_at <= self._clock():
            return None
        return AccessGrant(row.session_id, row.username)

    def check_refresh(self, token: str | None) -> AccessGrant:
        """Validate a refresh token without consuming it.

        A consumed token coming back means someone holds a copy: the whole
        session (refresh family) is revoked and ``reused`` is raised.
        """
        if not token or not token.startswith(REFRESH_PREFIX):
            raise RefreshError("invalid")
        row = self._store.get_token(hash_token(token))
        if row is None or row.kind != "refresh":
            raise RefreshError("invalid")
        if row.session_revoked_at is not None:
            raise RefreshError("revoked", username=row.username, session_id=row.session_id)
        if row.used_at is not None:
            self._store.revoke_session(row.session_id, "refresh_reuse")
            raise RefreshError("reused", username=row.username, session_id=row.session_id)
        if row.expires_at <= self._clock():
            raise RefreshError("expired", username=row.username, session_id=row.session_id)
        return AccessGrant(row.session_id, row.username)

    def refresh(self, token: str | None) -> tuple[AccessGrant, TokenPair]:
        """Consume ``token`` and issue a new pair in the same session."""
        grant = self.check_refresh(token)
        assert token is not None
        if not self._store.mark_token_used(hash_token(token)):
            self._store.revoke_session(grant.session_id, "refresh_reuse")
            raise RefreshError("reused", username=grant.username, session_id=grant.session_id)
        self._store.touch_session(grant.session_id)
        return grant, self._issue_pair(grant.session_id)

    def revoke_by_refresh(self, token: str | None, reason: str) -> AccessGrant | None:
        """Revoke the session a refresh token belongs to (logout without an access token)."""
        if not token or not token.startswith(REFRESH_PREFIX):
            return None
        row = self._store.get_token(hash_token(token))
        if row is None or row.kind != "refresh":
            return None
        self._store.revoke_session(row.session_id, reason)
        return AccessGrant(row.session_id, row.username)

    def revoke(self, session_id: str, reason: str) -> None:
        self._store.revoke_session(session_id, reason)

    def revoke_user(self, username: str, reason: str) -> int:
        return self._store.revoke_user_sessions(username, reason)
