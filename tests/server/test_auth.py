"""Tokens and store, identities and groups, rate limiting, password hashes."""

from __future__ import annotations

import os
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from mira_server.auth.backends import (
    DevBackend,
    hash_password,
    parse_password_hash,
    verify_password,
)
from mira_server.auth.identity import (
    AccessDeniedError,
    DevIdentityResolver,
    NssIdentityResolver,
    UnknownUserError,
)
from mira_server.auth.ratelimit import LoginRateLimiter
from mira_server.auth.sessions import RefreshError, SessionService, hash_token
from mira_server.config import parse_config
from mira_server.store import Store

# ── store and tokens ─────────────────────────────────────────────────


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    db = Store(tmp_path / "data" / "mira-server.sqlite3", clock=lambda: clock[0])
    yield db
    db.close()


@pytest.fixture
def sessions(store, clock):
    return SessionService(store, access_ttl=900, refresh_ttl=3600, clock=lambda: clock[0])


def test_store_is_private(store, tmp_path) -> None:
    assert stat.S_IMODE(os.stat(tmp_path / "data").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_existing_data_dir_is_tightened(tmp_path) -> None:
    data = tmp_path / "loose"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    Store(data / "db.sqlite3").close()
    assert stat.S_IMODE(os.stat(data).st_mode) == 0o700


def test_tokens_are_stored_hashed(sessions, store) -> None:
    _, pair = sessions.login("alice", "desktop")
    raw = sqlite3.connect(store.path).execute("SELECT token_hash FROM tokens").fetchall()
    hashes = {row[0] for row in raw}
    assert hashes == {hash_token(pair.access_token), hash_token(pair.refresh_token)}
    dump = "\n".join(sqlite3.connect(store.path).iterdump())
    assert pair.access_token not in dump and pair.refresh_token not in dump


def test_access_token_expires(sessions, clock) -> None:
    _, pair = sessions.login("alice", None)
    assert sessions.check_access(pair.access_token).username == "alice"
    clock[0] += 899
    assert sessions.check_access(pair.access_token) is not None
    clock[0] += 2
    assert sessions.check_access(pair.access_token) is None


def test_refresh_token_cannot_be_used_as_access_token(sessions) -> None:
    _, pair = sessions.login("alice", None)
    assert sessions.check_access(pair.refresh_token) is None
    with pytest.raises(RefreshError):
        sessions.refresh(pair.access_token)


def test_refresh_rotation_and_reuse_detection(sessions) -> None:
    session_id, first = sessions.login("alice", None)
    grant, second = sessions.refresh(first.refresh_token)
    assert grant.session_id == session_id and grant.username == "alice"
    assert sessions.check_access(second.access_token) is not None
    with pytest.raises(RefreshError) as info:
        sessions.refresh(first.refresh_token)
    assert info.value.reason == "reused"
    # The whole family is gone, including the tokens issued by the legitimate refresh.
    assert sessions.check_access(second.access_token) is None
    assert sessions.check_access(first.access_token) is None
    with pytest.raises(RefreshError) as info:
        sessions.refresh(second.refresh_token)
    assert info.value.reason == "revoked"


def test_refresh_token_expires(sessions, clock) -> None:
    _, pair = sessions.login("alice", None)
    clock[0] += 3601
    with pytest.raises(RefreshError) as info:
        sessions.refresh(pair.refresh_token)
    assert info.value.reason == "expired"


def test_revoke_user_affects_all_sessions(sessions) -> None:
    _, a = sessions.login("alice", None)
    _, b = sessions.login("alice", None)
    _, c = sessions.login("bob", None)
    assert sessions.revoke_user("alice", "test") == 2
    assert sessions.check_access(a.access_token) is None
    assert sessions.check_access(b.access_token) is None
    assert sessions.check_access(c.access_token) is not None


def test_purge_expired(sessions, store, clock) -> None:
    _, pair = sessions.login("alice", None)
    clock[0] += 4000
    store.purge_expired()
    assert store.get_token(hash_token(pair.refresh_token)) is None
    assert store.active_session_users() == []


def test_audit_records_metadata(store) -> None:
    store.audit("login.ok", username="alice", client_ip="10.0.0.1", session="abcd")
    [event] = store.audit_events("login.ok")
    assert event["username"] == "alice" and event["client_ip"] == "10.0.0.1"
    assert event["detail"] == {"session": "abcd"}


# ── identities and groups ────────────────────────────────────────────


def _pw(name, uid, gid, gecos="", home=None):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid, pw_gecos=gecos, pw_dir=home or f"/home/{name}")


GROUPS = {100: "users", 500: "mira-users", 501: "mira-admins", 1001: "alice", 1002: "bob", 1003: "carol",
          0: "root"}
MEMBERSHIP = {"alice": [1001, 500], "bob": [1002, 500, 501], "carol": [1003, 100], "root": [0, 500]}
PASSWD = {"alice": _pw("alice", 1001, 1001, "Alice Liddell,Room 1"), "bob": _pw("bob", 1002, 1002),
          "carol": _pw("carol", 1003, 1003), "root": _pw("root", 0, 0)}


def _resolver(membership=None):
    membership = membership if membership is not None else MEMBERSHIP

    def getpwnam(name):
        return PASSWD[name]

    def getgrgid(gid):
        if gid not in GROUPS:
            raise KeyError(gid)
        return SimpleNamespace(gr_name=GROUPS[gid])

    return NssIdentityResolver(
        "mira-users", "mira-admins",
        pwd_module=SimpleNamespace(getpwnam=getpwnam),
        grp_module=SimpleNamespace(getgrgid=getgrgid),
        getgrouplist=lambda name, gid: membership[name],
    )


def test_member_gets_user_role_and_gecos_name() -> None:
    identity = _resolver().resolve("alice")
    assert identity.roles == ("user",) and identity.role == "user"
    assert identity.display_name == "Alice Liddell"
    assert identity.uid == 1001 and identity.home == "/home/alice"


def test_admin_group_adds_admin_role() -> None:
    identity = _resolver().resolve("bob")
    assert identity.roles == ("user", "admin") and identity.is_admin
    assert identity.display_name == "bob"


def test_supplementary_group_is_enough() -> None:
    # Membership only through a supplementary group (not the primary gid).
    assert _resolver({**MEMBERSHIP, "alice": [1001, 100, 500]}).resolve("alice").name == "alice"


def test_admin_without_users_group_is_denied() -> None:
    with pytest.raises(AccessDeniedError):
        _resolver({**MEMBERSHIP, "bob": [1002, 501]}).resolve("bob")


def test_non_member_and_root_denied_unknown_user() -> None:
    resolver = _resolver()
    with pytest.raises(AccessDeniedError):
        resolver.resolve("carol")
    with pytest.raises(AccessDeniedError):
        resolver.resolve("root")
    with pytest.raises(UnknownUserError):
        resolver.resolve("nobody-here")
    with pytest.raises(UnknownUserError):
        resolver.resolve("-u")


def test_dev_resolver() -> None:
    cfg = parse_config({"auth": {"backend": "dev", "dev_users": [
        {"name": "dana", "password_hash": hash_password("x", n=1024), "admin": True}]}})
    identity = DevIdentityResolver(cfg.auth).resolve("dana")
    assert identity.is_admin and identity.uid == os.getuid()
    with pytest.raises(UnknownUserError):
        DevIdentityResolver(cfg.auth).resolve("eve")


# ── rate limiting ────────────────────────────────────────────────────


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = LoginRateLimiter(3, 60, clock=lambda: now[0])
    key = limiter.key("Alice", "10.0.0.1")
    assert key == ("alice", "10.0.0.1")
    for _ in range(3):
        assert limiter.retry_after(key) == 0
        limiter.record_failure(key)
    assert limiter.retry_after(key) == pytest.approx(60)
    assert limiter.retry_after(limiter.key("alice", "10.0.0.2")) == 0
    now[0] = 30
    assert limiter.retry_after(key) == pytest.approx(30)
    now[0] = 60
    assert limiter.retry_after(key) == 0


def test_rate_limiter_reset_on_success() -> None:
    limiter = LoginRateLimiter(1, 60)
    key = limiter.key("a", "ip")
    limiter.record_failure(key)
    assert limiter.retry_after(key) > 0
    limiter.reset(key)
    assert limiter.retry_after(key) == 0


# ── password hashes (dev backend) ────────────────────────────────────


def test_hash_and_verify() -> None:
    encoded = hash_password("s3cret", n=1024)
    assert encoded.startswith("scrypt$1024$8$1$")
    assert verify_password("s3cret", encoded)
    assert not verify_password("S3cret", encoded)
    assert not verify_password("s3cret", "garbage")
    assert hash_password("s3cret", n=1024) != encoded  # salted


@pytest.mark.parametrize("value", ["", "scrypt$1$2$3$4", "bcrypt$16384$8$1$AAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAA",
                                   "scrypt$1000$8$1$AAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAA"])
def test_parse_password_hash_rejects(value) -> None:
    with pytest.raises(ValueError):
        parse_password_hash(value)


async def test_dev_backend() -> None:
    cfg = parse_config({"auth": {"backend": "dev", "dev_users": [
        {"name": "dana", "password_hash": hash_password("pw", n=1024)}]}})
    backend = DevBackend(cfg.auth)
    assert await backend.verify("dana", "pw")
    assert not await backend.verify("dana", "nope")
    assert not await backend.verify("eve", "pw")
