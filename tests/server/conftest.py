"""Fixtures for Mira Server tests: a fake engine app behind a fake spawner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mira_server.app import create_server
from mira_server.auth.identity import AccessDeniedError, Identity, UnknownUserError
from mira_server.config import parse_config
from mira_server.engines import EngineManager
from mira_server.spawner.base import EngineHandle, Spawner, SpawnerError
from mira_server.spawner.netcheck import ListenerCheck
from mira_server.store import Store

PASSWORD = "correct horse"


# ── fake engine ──────────────────────────────────────────────────────


@dataclass
class EngineLog:
    requests: list[dict[str, Any]] = field(default_factory=list)
    ws_frames: list[str] = field(default_factory=list)
    ws_sockets: list[web.WebSocketResponse] = field(default_factory=list)
    ws_paths: list[str] = field(default_factory=list)
    team_layers: list[Any] = field(default_factory=list)


def make_engine_app(token: str, log: EngineLog) -> web.Application:
    open_paths = {"/health", "/version", "/api/version"}

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        log.requests.append({"path": request.path, "query": request.rel_url.raw_query_string,
                             "headers": dict(request.headers)})
        presented = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() \
            or request.query.get("access_token", "")
        if request.path not in open_paths and presented != token:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "mira-gateway"})

    async def version(_: web.Request) -> web.Response:
        return web.json_response({"service": "mira-gateway", "agent_version": "9.9.9", "api_contract": "v1"})

    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        return web.json_response({
            "method": request.method,
            "path": request.path,
            "query": request.rel_url.raw_query_string,
            "headers": {k: v for k, v in request.headers.items()},
            "body_len": len(body),
        }, headers={"Set-Cookie": "engine=1", "Access-Control-Allow-Origin": "*", "X-Engine": "yes"})

    async def upload(request: web.Request) -> web.Response:
        digest = hashlib.sha256()
        size = 0
        async for chunk in request.content.iter_any():
            digest.update(chunk)
            size += len(chunk)
        return web.json_response({"size": size, "sha256": digest.hexdigest()})

    async def stream(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for i in range(3):
            await resp.write(f"data: {i}\n\n".encode())
            await asyncio.sleep(0.05)
        await resp.write_eof()
        return resp

    async def team_layer(request: web.Request) -> web.Response:
        body = await request.json()
        log.team_layers.append(body["layer"])
        return web.json_response({"ok": True, "revision": (body["layer"] or {}).get("revision", "")})

    async def ws(request: web.Request) -> web.WebSocketResponse:
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        log.ws_sockets.append(sock)
        log.ws_paths.append(request.path)
        async for msg in sock:
            if msg.type == web.WSMsgType.TEXT:
                log.ws_frames.append(msg.data)
                await sock.send_str(json.dumps({"echo": msg.data}))
            elif msg.type == web.WSMsgType.BINARY:
                await sock.send_bytes(msg.data[::-1])
        return sock

    app = web.Application(middlewares=[auth])
    app.router.add_get("/health", health)
    app.router.add_get("/version", version)
    app.router.add_get("/api/version", version)
    app.router.add_route("*", "/api/echo", echo)
    app.router.add_post("/api/upload", upload)
    app.router.add_get("/api/stream", stream)
    app.router.add_put("/api/team/layer", team_layer)
    app.router.add_get("/ws", ws)
    app.router.add_get("/ws/workflow/v1", ws)
    return app


class FakeSpawner(Spawner):
    """Runs a fake engine per user in-process."""

    def __init__(self) -> None:
        self.servers: dict[str, TestServer] = {}
        self.logs: dict[str, EngineLog] = {}
        self.tokens: dict[str, str] = {}
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.status_calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.fail_with: SpawnerError | None = None
        self.stop_fails_with: SpawnerError | None = None

    async def _launch(self, name: str) -> EngineHandle:
        token = f"engine-token-{name}-{len(self.starts)}"
        log = self.logs.setdefault(name, EngineLog())
        server = TestServer(make_engine_app(token, log))
        await server.start_server()
        self.servers[name] = server
        self.tokens[name] = token
        return EngineHandle(port=server.port, token=token, pid=os.getpid(), version="9.9.9", started=True)

    async def start(self, identity: Identity) -> EngineHandle:
        self.starts.append(identity.name)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        if identity.name in self.servers:
            server = self.servers[identity.name]
            return EngineHandle(port=server.port, token=self.tokens[identity.name], pid=os.getpid(),
                                version="9.9.9", started=False)
        return await self._launch(identity.name)

    async def status(self, identity: Identity) -> EngineHandle | None:
        self.status_calls.append(identity.name)
        server = self.servers.get(identity.name)
        if server is None:
            return None
        return EngineHandle(port=server.port, token=self.tokens[identity.name], pid=os.getpid(),
                            version="9.9.9", started=False)

    async def stop(self, identity: Identity) -> None:
        self.stops.append(identity.name)
        if self.stop_fails_with is not None:
            raise self.stop_fails_with
        server = self.servers.pop(identity.name, None)
        if server is not None:
            await server.close()

    def expected_uid(self, identity: Identity) -> int:
        return identity.uid

    async def close(self) -> None:
        for server in list(self.servers.values()):
            await server.close()
        self.servers.clear()


# ── fake identities and passwords ────────────────────────────────────


class FakeResolver:
    def __init__(self) -> None:
        self.users: dict[str, Identity] = {
            "alice": Identity("alice", 1001, "/home/alice", "Alice A", ("user",)),
            "bob": Identity("bob", 1002, "/home/bob", "Bob B", ("user", "admin")),
            # An administration account outside mira-users: no engine.
            "dave": Identity("dave", 1004, "/home/dave", "Dave D", ("admin",)),
        }
        self.outsiders = {"carol"}
        self.calls = 0
        self.broken = False  # simulate a directory (LDAP/SSSD) outage

    def resolve(self, username: str) -> Identity:
        self.calls += 1
        if self.broken:
            raise OSError("directory unreachable")
        if username in self.users:
            return self.users[username]
        if username in self.outsiders:
            raise AccessDeniedError(f"{username} is not a member of mira-users")
        raise UnknownUserError(username)


class FakeBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, username: str, password: str) -> bool:
        self.calls += 1
        return password == PASSWORD and username in {"alice", "bob", "carol", "dave"}


def ok_listener(port: int, uid: int, *, pid: int | None = None) -> ListenerCheck:
    return ListenerCheck(True, uids=(uid,))


@dataclass
class Harness:
    client: TestClient
    server: Any
    spawner: FakeSpawner
    resolver: FakeResolver
    backend: FakeBackend
    store: Store

    async def login(self, username: str = "alice", password: str = PASSWORD) -> dict[str, Any]:
        resp = await self.client.post("/server/auth/login",
                                      json={"username": username, "password": password, "client": "desktop"})
        assert resp.status == 200, await resp.text()
        return await resp.json()

    @staticmethod
    def bearer(tokens: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens['access_token']}"}

    async def wait_running(self, tokens: dict[str, Any]) -> None:
        for _ in range(100):
            resp = await self.client.get("/server/me", headers=self.bearer(tokens))
            data = await resp.json()
            if data["engine"]["state"] == "running":
                return
            if self.server.engines.get(data["user"]["name"]).state == "stopped":
                await self.client.post("/server/engine/start", headers=self.bearer(tokens))
            await asyncio.sleep(0.02)
        raise AssertionError("engine did not reach running")


def make_config(tmp_path, **sections: dict[str, Any]):
    data: dict[str, Any] = {
        "server": {"data_dir": str(tmp_path / "data"), "public_url": "https://mira.example.com"},
        "auth": {},
        "spawner": {},
    }
    for key, value in sections.items():
        data[key].update(value)
    return parse_config(data)


@pytest.fixture
async def harness_factory(tmp_path):
    created: list[Harness] = []

    async def build(*, config=None, listener_check=ok_listener, idle_timeout: float = 0,
                    clock=None, start_grace: float = 0.0, dev: bool = False, **config_sections) -> Harness:
        cfg = config or make_config(tmp_path, **config_sections)
        store = Store(cfg.db_path)
        spawner = FakeSpawner()
        options: dict[str, Any] = {"listener_check": listener_check}
        if clock is not None:
            options["clock"] = clock
        engines = EngineManager(spawner, store, idle_timeout=idle_timeout, verify_listener=True, **options)
        resolver = FakeResolver()
        backend = FakeBackend()
        server = create_server(cfg, store=store, backend=backend, resolver=resolver, engines=engines,
                               sweep_interval=3600, start_grace=start_grace, dev=dev)
        client = TestClient(TestServer(server.build_app()))
        await client.start_server()
        harness = Harness(client, server, spawner, resolver, backend, store)
        created.append(harness)
        return harness

    yield build

    for harness in created:
        await harness.client.close()
        await harness.spawner.close()
        harness.store.close()


@pytest.fixture
async def harness(harness_factory):
    return await harness_factory()
