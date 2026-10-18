"""Mira Server HTTP API, proxy and WebSocket behaviour against a fake engine."""

from __future__ import annotations

import asyncio
import hashlib
import json

import aiohttp
import pytest
from aiohttp import WSServerHandshakeError

from mira_server.spawner.base import CONFLICT, START_FAILED, SpawnerError
from mira_server.spawner.netcheck import ListenerCheck

from .conftest import PASSWORD, Harness

# ── /server/info and auth ────────────────────────────────────────────


async def test_info(harness: Harness) -> None:
    resp = await harness.client.get("/server/info")
    assert resp.status == 200
    data = await resp.json()
    assert data["product"] == "mira-server"
    assert data["protocol"] == 1
    assert data["auth"] == {"methods": ["password"]}
    assert isinstance(data["version"], str)


async def test_login_shape(harness: Harness) -> None:
    data = await harness.login("bob")
    assert data["access_token"].startswith("msa_") and data["refresh_token"].startswith("msr_")
    assert data["expires_in"] == 900 and data["refresh_expires_in"] == 30 * 86400
    assert data["user"] == {"name": "bob", "display_name": "Bob B", "roles": ["user", "admin"]}
    assert [e["event"] for e in harness.store.audit_events("login.ok")] == ["login.ok"]


async def test_login_wrong_password_is_401(harness: Harness) -> None:
    resp = await harness.client.post("/server/auth/login", json={"username": "alice", "password": "nope"})
    assert resp.status == 401
    assert (await resp.json())["error"] == "unauthorized"


async def test_login_outside_users_group_is_403(harness: Harness) -> None:
    resp = await harness.client.post("/server/auth/login", json={"username": "carol", "password": PASSWORD})
    assert resp.status == 403
    assert (await resp.json())["error"] == "forbidden"


@pytest.mark.parametrize("body", [{}, {"username": "alice"}, {"username": 1, "password": "x"}, ["x"]])
async def test_login_bad_body_is_400(harness: Harness, body) -> None:
    resp = await harness.client.post("/server/auth/login", json=body)
    assert resp.status == 400


async def test_login_invalid_username_never_reaches_backend(harness: Harness) -> None:
    resp = await harness.client.post("/server/auth/login", json={"username": "-oops", "password": PASSWORD})
    assert resp.status == 401
    assert harness.backend.calls == 0


async def test_login_rate_limited_per_user_and_ip(harness_factory) -> None:
    harness = await harness_factory(auth={"login_max_attempts": 3, "login_window": 60})
    for _ in range(3):
        resp = await harness.client.post("/server/auth/login", json={"username": "alice", "password": "bad"})
        assert resp.status == 401
    resp = await harness.client.post("/server/auth/login", json={"username": "alice", "password": PASSWORD})
    assert resp.status == 429
    assert (await resp.json())["error"] == "too_many_attempts"
    assert int(resp.headers["Retry-After"]) >= 1
    # Another user from the same IP is unaffected.
    assert (await harness.login("bob"))["user"]["name"] == "bob"


async def test_refresh_rotates_and_reuse_revokes_family(harness: Harness) -> None:
    first = await harness.login()
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status == 200
    second = await resp.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["user"]["name"] == "alice"
    me = await harness.client.get("/server/me", headers=harness.bearer(second))
    assert me.status == 200

    # Replaying the consumed refresh token revokes the whole session.
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status == 401
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert resp.status == 401
    me = await harness.client.get("/server/me", headers=harness.bearer(second))
    assert me.status == 401
    assert harness.store.audit_events("security.refresh_reuse")


async def test_refresh_with_garbage_is_401(harness: Harness) -> None:
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": "msr_nope"})
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_logout_revokes(harness: Harness) -> None:
    tokens = await harness.login()
    resp = await harness.client.post("/server/auth/logout", headers=harness.bearer(tokens))
    assert resp.status == 204
    resp = await harness.client.get("/server/me", headers=harness.bearer(tokens))
    assert resp.status == 401
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 401


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer msa_wrong"}, {"Authorization": "Basic x"}])
async def test_me_requires_token(harness: Harness, headers) -> None:
    resp = await harness.client.get("/server/me", headers=headers)
    assert resp.status == 401
    assert (await resp.json())["error"] == "unauthorized"


async def test_server_routes_ignore_query_token(harness: Harness) -> None:
    tokens = await harness.login()
    resp = await harness.client.get(f"/server/me?access_token={tokens['access_token']}")
    assert resp.status == 401


async def test_me_initial_engine_state(harness: Harness) -> None:
    tokens = await harness.login()
    resp = await harness.client.get("/server/me", headers=harness.bearer(tokens))
    assert await resp.json() == {
        "user": {"name": "alice", "display_name": "Alice A", "roles": ["user"]},
        "engine": {"state": "stopped", "version": None, "message": None},
    }


async def test_unknown_route_is_json_404(harness: Harness) -> None:
    resp = await harness.client.get("/nope")
    assert resp.status == 404
    assert (await resp.json())["error"] == "not_found"


# ── group changes ────────────────────────────────────────────────────


async def test_group_removal_revokes_sessions_and_stops_engine(harness_factory) -> None:
    harness = await harness_factory(auth={"group_recheck_interval": 0})
    tokens = await harness.login()
    await harness.wait_running(tokens)
    harness.resolver.users.pop("alice")
    harness.resolver.outsiders.add("alice")

    resp = await harness.client.get("/server/me", headers=harness.bearer(tokens))
    assert resp.status == 403
    assert (await resp.json())["error"] == "forbidden"
    await harness.server.engines.drain()
    assert harness.spawner.stops == ["alice"]
    assert harness.server.engines.get("alice").state == "stopped"
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 401
    assert harness.store.audit_events("access.revoked")


async def test_group_recheck_is_rate_limited(harness_factory) -> None:
    harness = await harness_factory(auth={"group_recheck_interval": 3600})
    tokens = await harness.login()
    calls = harness.resolver.calls
    for _ in range(3):
        assert (await harness.client.get("/server/me", headers=harness.bearer(tokens))).status == 200
    assert harness.resolver.calls == calls
    # Refresh always re-checks.
    await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert harness.resolver.calls == calls + 1


async def test_failed_stop_after_removal_forgets_engine(harness_factory) -> None:
    harness = await harness_factory(auth={"group_recheck_interval": 0})
    tokens = await harness.login()
    await harness.wait_running(tokens)
    harness.spawner.stop_fails_with = SpawnerError("launcher_denied", "sudo refused")
    harness.resolver.users.pop("alice")
    harness.resolver.outsiders.add("alice")
    assert (await harness.client.get("/server/me", headers=harness.bearer(tokens))).status == 403
    await harness.server.engines.drain()
    engine = harness.server.engines.get("alice")
    assert engine.state == "stopped" and engine.handle is None
    assert harness.store.audit_events("engine.forgotten")


# ── engine start / stop and /api proxy ───────────────────────────────


async def test_api_returns_503_while_starting_and_shares_one_start(harness: Harness) -> None:
    tokens = await harness.login()
    harness.spawner.gate = asyncio.Event()
    responses = await asyncio.gather(*[
        harness.client.get("/api/echo", headers=harness.bearer(tokens)) for _ in range(5)
    ])
    for resp in responses:
        assert resp.status == 503
        assert resp.headers["Retry-After"] == "2"
        assert (await resp.json())["error"] == "engine_starting"
    me = await (await harness.client.get("/server/me", headers=harness.bearer(tokens))).json()
    assert me["engine"]["state"] == "starting"
    assert harness.spawner.starts == ["alice"]

    harness.spawner.gate.set()
    await harness.server.engines.drain()
    resp = await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    assert resp.status == 200
    me = await (await harness.client.get("/server/me", headers=harness.bearer(tokens))).json()
    assert me["engine"] == {"state": "running", "version": "9.9.9", "message": None}
    assert harness.spawner.starts == ["alice"]


async def test_engine_start_and_stop_endpoints(harness_factory) -> None:
    harness = await harness_factory(start_grace=2.0)
    tokens = await harness.login()
    resp = await harness.client.post("/server/engine/start", headers=harness.bearer(tokens))
    assert resp.status == 200
    assert await resp.json() == {"state": "running", "version": "9.9.9", "message": None}
    resp = await harness.client.post("/server/engine/stop", headers=harness.bearer(tokens))
    assert await resp.json() == {"state": "stopped", "version": None, "message": None}
    assert harness.spawner.stops == ["alice"]
    events = [e["event"] for e in harness.store.audit_events()]
    assert "engine.started" in events and "engine.stopped" in events


async def test_proxy_rewrites_headers_and_query(harness: Harness) -> None:
    tokens = await harness.login("bob")
    await harness.wait_running(tokens)
    resp = await harness.client.post(
        f"/api/echo?x=1&access_token={tokens['access_token']}&y=a%20b",
        headers={**harness.bearer(tokens), "Cookie": "c=1", "Origin": "file://",
                 "X-Mira-Server-User": "mallory", "X-Mira-Request-Id": "client-id", "X-Custom": "kept",
                 "Connection": "keep-alive, X-Hop", "X-Hop": "drop-me"},
        data=b"hello",
    )
    assert resp.status == 200
    data = await resp.json()
    sent = {k.lower(): v for k, v in data["headers"].items()}
    assert data["path"] == "/api/echo"
    assert data["query"] == "x=1&y=a%20b"
    assert sent["authorization"] == f"Bearer {harness.spawner.tokens['bob']}"
    assert sent["x-mira-server-user"] == "bob"
    assert sent["x-mira-server-role"] == "admin"
    assert sent["x-mira-request-id"] == resp.headers["X-Mira-Request-Id"] != "client-id"
    assert sent["x-custom"] == "kept"
    for gone in ("cookie", "origin", "x-hop"):
        assert gone not in sent
    assert data["body_len"] == 5
    # Engine response headers: cookies and the engine's own CORS headers are dropped.
    assert "Set-Cookie" not in resp.headers
    assert resp.headers["X-Engine"] == "yes"
    assert resp.headers["Access-Control-Allow-Origin"] == "file://"


async def test_proxy_accepts_query_token_for_api(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    resp = await harness.client.get(f"/api/echo?access_token={tokens['access_token']}")
    assert resp.status == 200
    assert (await resp.json())["query"] == ""


async def test_proxy_streams_responses_and_uploads(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    resp = await harness.client.get("/api/stream", headers=harness.bearer(tokens))
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    chunks = []
    async for chunk in resp.content.iter_any():
        chunks.append(chunk)
    assert b"".join(chunks) == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"

    payload = bytes(range(256)) * 8192  # 2 MiB

    async def body():
        for i in range(0, len(payload), 65536):
            yield payload[i:i + 65536]

    resp = await harness.client.post("/api/upload", headers=harness.bearer(tokens), data=body())
    assert resp.status == 200
    assert await resp.json() == {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


async def test_users_reach_only_their_own_engine(harness: Harness) -> None:
    alice = await harness.login("alice")
    bob = await harness.login("bob")
    await harness.wait_running(alice)
    await harness.wait_running(bob)
    await harness.client.get("/api/echo", headers=harness.bearer(alice))
    await harness.client.get("/api/echo", headers=harness.bearer(bob))
    alice_seen = {r["headers"].get("X-Mira-Server-User") for r in harness.spawner.logs["alice"].requests}
    bob_seen = {r["headers"].get("X-Mira-Server-User") for r in harness.spawner.logs["bob"].requests}
    assert "bob" not in alice_seen and "alice" in alice_seen
    assert "alice" not in bob_seen and "bob" in bob_seen


async def test_conflict_is_409(harness: Harness) -> None:
    tokens = await harness.login()
    harness.spawner.fail_with = SpawnerError(CONFLICT, "engine already running on host other")
    await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    await harness.server.engines.drain()
    resp = await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    assert resp.status == 409
    assert await resp.json() == {"error": "engine_conflict", "message": "engine already running on host other"}
    me = await (await harness.client.get("/server/me", headers=harness.bearer(tokens))).json()
    assert me["engine"]["state"] == "conflict"


async def test_failed_start_is_503_unavailable_then_explicit_start_retries(harness: Harness) -> None:
    tokens = await harness.login()
    harness.spawner.fail_with = SpawnerError(START_FAILED, "the engine exited with code 1")
    await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    await harness.server.engines.drain()
    resp = await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    assert resp.status == 503
    assert (await resp.json())["error"] == "engine_unavailable"
    assert len(harness.spawner.starts) == 1  # backoff: no retry storm
    harness.spawner.fail_with = None
    await harness.client.post("/server/engine/start", headers=harness.bearer(tokens))
    await harness.server.engines.drain()
    assert (await harness.client.get("/api/echo", headers=harness.bearer(tokens))).status == 200


async def test_listener_mismatch_refuses_and_never_sends_token(harness_factory) -> None:
    def squatted(port: int, uid: int, *, pid=None) -> ListenerCheck:
        return ListenerCheck(False, f"port {port} is held by uid(s) [4242], expected {uid}", (4242,))

    harness = await harness_factory(listener_check=squatted)
    tokens = await harness.login()
    await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    await harness.server.engines.drain()
    resp = await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    assert resp.status == 503
    assert (await resp.json())["error"] == "engine_unavailable"
    events = harness.store.audit_events("security.listener_mismatch")
    assert events and events[0]["detail"]["uids"] == [4242]
    assert harness.spawner.logs["alice"].requests == []  # not even /health


async def test_engine_token_change_triggers_reattach(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    harness.spawner.tokens["alice"] = "rotated"  # the engine restarted with another token
    engine = harness.server.engines.get("alice")
    engine.handle = type(engine.handle)(**{**engine.handle.__dict__, "token": "stale"})
    resp = await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    assert resp.status == 503
    assert (await resp.json())["error"] == "engine_starting"
    await harness.server.engines.drain()
    assert (await harness.client.get("/api/version", headers=harness.bearer(tokens))).status == 200


async def test_engine_gone_triggers_restart(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    await harness.spawner.servers.pop("alice").close()
    resp = await harness.client.get("/api/echo", headers=harness.bearer(tokens))
    assert resp.status == 503
    assert (await resp.json())["error"] == "engine_starting"
    await harness.server.engines.drain()
    assert (await harness.client.get("/api/echo", headers=harness.bearer(tokens))).status == 200
    assert harness.spawner.starts == ["alice", "alice"]


async def test_reattach_after_restart_uses_status(harness: Harness) -> None:
    from mira_server.engines import EngineManager

    from .conftest import ok_listener

    tokens = await harness.login()
    await harness.wait_running(tokens)
    assert harness.store.get_engine("alice").started_by_server

    # A fresh engine manager on the same store, as after a Mira Server restart.
    restarted = EngineManager(harness.spawner, harness.store, idle_timeout=0, listener_check=ok_listener)
    restarted.http = harness.server.http
    starts = list(harness.spawner.starts)
    await restarted.reattach_all(harness.server.resolve_or_none)
    engine = restarted.get("alice")
    assert engine.state == "running" and engine.started_by_server
    assert harness.spawner.status_calls == ["alice"]
    assert harness.spawner.starts == starts


async def test_reattach_drops_records_of_stopped_engines(harness: Harness) -> None:
    from mira_server.engines import EngineManager

    from .conftest import ok_listener

    tokens = await harness.login()
    await harness.wait_running(tokens)
    await harness.spawner.servers.pop("alice").close()
    restarted = EngineManager(harness.spawner, harness.store, idle_timeout=0, listener_check=ok_listener)
    restarted.http = harness.server.http
    await restarted.reattach_all(harness.server.resolve_or_none)
    assert restarted.get("alice").state == "stopped"
    assert harness.store.get_engine("alice") is None


# ── WebSocket proxy ──────────────────────────────────────────────────


async def test_ws_overwrites_user_id_and_passes_frames(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    async with harness.client.ws_connect(f"/ws?access_token={tokens['access_token']}",
                                         headers={"Origin": "file://"}) as ws:
        await ws.send_str(json.dumps({"type": "chat", "user_id": "mallory", "content": "hi"}))
        reply = json.loads((await ws.receive(timeout=5)).data)
        assert json.loads(reply["echo"]) == {"type": "chat", "user_id": "alice", "content": "hi"}
        await ws.send_str("plain text")
        assert json.loads((await ws.receive(timeout=5)).data) == {"echo": "plain text"}
        await ws.send_str("[1, 2]")
        assert json.loads((await ws.receive(timeout=5)).data) == {"echo": "[1, 2]"}
        await ws.send_bytes(b"abc")
        assert (await ws.receive(timeout=5)).data == b"cba"
    frames = harness.spawner.logs["alice"].ws_frames
    assert "mallory" not in frames[0]
    ws_request = [r for r in harness.spawner.logs["alice"].requests if r["path"] == "/ws"][0]
    assert ws_request["query"] == f"access_token={harness.spawner.tokens['alice']}"
    assert ws_request["headers"]["X-Mira-Server-User"] == "alice"


async def test_ws_accepts_bearer_header(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    async with harness.client.ws_connect("/ws", headers=harness.bearer(tokens)) as ws:
        await ws.send_str("{}")
        assert json.loads((await ws.receive(timeout=5)).data) == {"echo": '{"user_id": "alice"}'}


@pytest.mark.parametrize("path,headers,status", [
    ("/ws", {}, 401),
    ("/ws?access_token=msa_bad", {}, 401),
    ("/ws?access_token={token}", {"Origin": "https://evil.example"}, 403),
])
async def test_ws_rejections(harness: Harness, path, headers, status) -> None:
    tokens = await harness.login()
    with pytest.raises(WSServerHandshakeError) as info:
        await harness.client.ws_connect(path.format(token=tokens["access_token"]), headers=headers)
    assert info.value.status == status


async def test_ws_closes_1012_when_engine_drops(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    async with harness.client.ws_connect(f"/ws?access_token={tokens['access_token']}") as ws:
        await ws.send_str("{}")
        await ws.receive(timeout=5)
        engine_ws = harness.spawner.logs["alice"].ws_sockets[-1]
        await engine_ws.close()
        msg = await ws.receive(timeout=5)
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert msg.data == 1012


async def test_ws_closes_1001_on_engine_stop(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    async with harness.client.ws_connect(f"/ws?access_token={tokens['access_token']}") as ws:
        await ws.send_str("{}")
        await ws.receive(timeout=5)
        await harness.client.post("/server/engine/stop", headers=harness.bearer(tokens))
        msg = await ws.receive(timeout=5)
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert msg.data == 1001


async def test_ws_503_while_starting(harness: Harness) -> None:
    tokens = await harness.login()
    harness.spawner.gate = asyncio.Event()
    with pytest.raises(WSServerHandshakeError) as info:
        await harness.client.ws_connect(f"/ws?access_token={tokens['access_token']}")
    assert info.value.status == 503
    harness.spawner.gate.set()


# ── CORS and origins ─────────────────────────────────────────────────


@pytest.mark.parametrize("origin", ["file://", "https://mira.example.com"])
async def test_preflight_for_allowed_origins(harness: Harness, origin: str) -> None:
    resp = await harness.client.options("/api/config", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization, content-type"})
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Origin"] == origin
    assert "authorization" in resp.headers["Access-Control-Allow-Headers"].lower()
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]


async def test_disallowed_origin_is_rejected(harness: Harness) -> None:
    resp = await harness.client.options("/server/auth/login", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert resp.status == 403
    assert "Access-Control-Allow-Origin" not in resp.headers
    resp = await harness.client.post("/server/auth/login", headers={"Origin": "https://evil.example"},
                                     json={"username": "alice", "password": PASSWORD})
    assert resp.status == 403
    assert (await resp.json())["error"] == "origin_not_allowed"
    assert harness.backend.calls == 0


async def test_cors_headers_on_errors(harness: Harness) -> None:
    resp = await harness.client.get("/server/me", headers={"Origin": "file://"})
    assert resp.status == 401
    assert resp.headers["Access-Control-Allow-Origin"] == "file://"
    assert "X-Mira-Request-Id" in resp.headers["Access-Control-Expose-Headers"]


async def test_configured_origins_replace_defaults(harness_factory) -> None:
    harness = await harness_factory(server={"allowed_origins": ["http://localhost:5173"]})
    ok = await harness.client.get("/server/info", headers={"Origin": "http://localhost:5173"})
    assert ok.status == 200
    denied = await harness.client.get("/server/info", headers={"Origin": "file://"})
    assert denied.status == 403


# ── idle culling ─────────────────────────────────────────────────────


async def test_idle_culling_stops_only_idle_server_started_engines(harness_factory) -> None:
    now = [1000.0]
    harness = await harness_factory(idle_timeout=60, clock=lambda: now[0])
    alice = await harness.login("alice")
    bob = await harness.login("bob")
    await harness.wait_running(alice)
    await harness.wait_running(bob)
    engines = harness.server.engines
    engines.get("bob").started_by_server = False  # e.g. attached to a Remote-SSH engine

    now[0] += 30
    assert await engines.cull_idle() == []
    async with harness.client.ws_connect(f"/ws?access_token={alice['access_token']}"):
        now[0] += 120
        assert await engines.cull_idle() == []  # an open WebSocket keeps it alive
    now[0] += 120
    assert await engines.cull_idle() == ["alice"]
    assert harness.spawner.stops == ["alice"]
    assert engines.get("bob").state == "running"
    assert [e["detail"]["reason"] for e in harness.store.audit_events("engine.stopped")] == ["idle"]


async def test_idle_timeout_zero_disables_culling(harness_factory) -> None:
    now = [0.0]
    harness = await harness_factory(idle_timeout=0, clock=lambda: now[0])
    tokens = await harness.login()
    await harness.wait_running(tokens)
    now[0] += 10**9
    assert await harness.server.engines.cull_idle() == []


# ── contract additions agreed with the desktop client (mira-ui#85) ───


async def test_origin_null_is_allowed(harness: Harness) -> None:
    resp = await harness.client.options("/server/auth/login", headers={
        "Origin": "null", "Access-Control-Request-Method": "POST"})
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "null"
    for header in ("Authorization", "Content-Type", "X-Mira-Request-Id"):
        assert header in resp.headers["Access-Control-Allow-Headers"]


@pytest.mark.parametrize("dev,status", [(True, 200), (False, 403)])
async def test_vite_dev_origins_only_in_dev_mode(harness_factory, dev, status) -> None:
    harness = await harness_factory(dev=dev)
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        resp = await harness.client.get("/server/info", headers={"Origin": origin})
        assert resp.status == status


async def test_retry_after_is_exposed_on_503(harness: Harness) -> None:
    tokens = await harness.login()
    harness.spawner.gate = asyncio.Event()
    resp = await harness.client.get("/api/echo", headers={**harness.bearer(tokens), "Origin": "file://"})
    assert resp.status == 503
    assert resp.headers["Retry-After"] == "2"
    assert resp.headers["Access-Control-Allow-Origin"] == "file://"
    assert "Retry-After" in resp.headers["Access-Control-Expose-Headers"]
    harness.spawner.gate.set()


async def test_query_token_only_for_get_and_head(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    query = f"?access_token={tokens['access_token']}"
    assert (await harness.client.head(f"/api/echo{query}")).status == 200
    resp = await harness.client.post(f"/api/echo{query}", data=b"x")
    assert resp.status == 401


async def test_tokens_never_logged(harness: Harness, caplog) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    with caplog.at_level("DEBUG"):
        await harness.client.get(f"/api/echo?access_token={tokens['access_token']}&x=1")
        async with harness.client.ws_connect(f"/ws?access_token={tokens['access_token']}") as ws:
            await ws.send_str("{}")
            await ws.receive(timeout=5)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "GET /api/echo 200" in text
    assert tokens["access_token"] not in text
    assert harness.spawner.tokens["alice"] not in text


async def test_workflow_ws_path_is_proxied(harness: Harness) -> None:
    tokens = await harness.login()
    await harness.wait_running(tokens)
    async with harness.client.ws_connect(f"/ws/workflow/v1?access_token={tokens['access_token']}",
                                         headers={"Origin": "file://"}) as ws:
        await ws.send_str(json.dumps({"message_type": "subscribe", "project_id": "p1"}))
        echoed = json.loads(json.loads((await ws.receive(timeout=5)).data)["echo"])
    assert echoed == {"message_type": "subscribe", "project_id": "p1", "user_id": "alice"}
    assert harness.spawner.logs["alice"].ws_paths == ["/ws/workflow/v1"]
    with pytest.raises(WSServerHandshakeError) as info:
        await harness.client.ws_connect(f"/ws/workflow/v1?access_token={tokens['access_token']}",
                                        headers={"Origin": "https://evil.example"})
    assert info.value.status == 403


async def test_ws_starts_a_stopped_engine(harness_factory) -> None:
    harness = await harness_factory(start_grace=2.0)
    tokens = await harness.login()
    async with harness.client.ws_connect(f"/ws?access_token={tokens['access_token']}") as ws:
        await ws.send_str("{}")
        assert json.loads((await ws.receive(timeout=5)).data) == {"echo": '{"user_id": "alice"}'}
    assert harness.spawner.starts == ["alice"]


async def test_refresh_transient_failure_keeps_token_usable(harness: Harness) -> None:
    tokens = await harness.login()
    harness.resolver.broken = True
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 503
    assert (await resp.json())["error"] == "unavailable"
    harness.resolver.broken = False
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 200  # not consumed by the failed attempt, so no reuse detection
    assert not harness.store.audit_events("security.refresh_reuse")
    assert not harness.store.audit_events("access.revoked")


async def test_refresh_definitive_failures(harness: Harness) -> None:
    tokens = await harness.login()
    resp = await harness.client.post("/server/auth/refresh", data=b"not json")
    assert resp.status == 400
    harness.resolver.users.pop("alice")
    harness.resolver.outsiders.add("alice")
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 403
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 401


async def test_logout_with_refresh_token_only(harness: Harness) -> None:
    tokens = await harness.login()
    resp = await harness.client.post("/server/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 204
    assert (await harness.client.get("/server/me", headers=harness.bearer(tokens))).status == 401
    resp = await harness.client.post("/server/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status == 401
    resp = await harness.client.post("/server/auth/logout", json={"refresh_token": "msr_unknown"})
    assert resp.status == 401
    assert (await harness.client.post("/server/auth/logout")).status == 401
