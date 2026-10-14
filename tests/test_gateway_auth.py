"""UI gateway access control: token, origin policy, bind default and token storage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger
from typer.testing import CliRunner

from mira_engine.bus.queue import MessageBus
from mira_engine.channels import ui as ui_channel_mod
from mira_engine.channels.base import BaseChannel
from mira_engine.channels.ui import UiChannel
from mira_engine.cli import agent_service as svc
from mira_engine.config.schema import Config, GatewayConfig, UiChannelConfig
from mira_engine.security import gateway_auth
from mira_engine.security.gateway_auth import (
    DEFAULT_ALLOWED_ORIGINS,
    ensure_token_file,
    gateway_token_path,
    local_gateway_tokens,
    resolve_gateway_token,
)

TOKEN = "test-gateway-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _minimal_base_init(self, config, bus) -> None:
    self.config = config
    self.bus = bus
    self._running = False


def _make_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> UiChannel:
    runtime_root = tmp_path / "runtime-subdirs"
    monkeypatch.setattr(
        ui_channel_mod,
        "get_runtime_subdir",
        lambda name: (runtime_root / name).mkdir(parents=True, exist_ok=True) or (runtime_root / name),
    )
    with patch.object(BaseChannel, "__init__", _minimal_base_init):
        with patch.object(ui_channel_mod, "_load_ui_instructions", return_value=""):
            return UiChannel(UiChannelConfig(), MagicMock(spec=MessageBus), workspace=tmp_path, **kwargs)


@pytest.fixture
def channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UiChannel:
    monkeypatch.delenv(gateway_auth.TOKEN_ENV, raising=False)
    monkeypatch.delenv(gateway_auth.ALLOWED_ORIGINS_ENV, raising=False)
    return _make_channel(tmp_path, monkeypatch, auth_token=TOKEN)


@pytest.fixture
async def client(channel: UiChannel):
    test_client = TestClient(TestServer(channel._build_app()))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


# ── HTTP token ───────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
async def test_api_rejects_missing_or_wrong_token(client: TestClient, headers: dict) -> None:
    resp = await client.get("/api/projects", headers=headers)
    assert resp.status == 401
    assert await resp.json() == {"error": "unauthorized"}
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_api_accepts_bearer_header(client: TestClient) -> None:
    resp = await client.get("/api/projects", headers=AUTH)
    assert resp.status == 200


async def test_api_accepts_query_token_for_resource_urls(client: TestClient) -> None:
    assert (await client.get("/api/projects", params={"access_token": TOKEN})).status == 200
    assert (await client.get("/api/projects", params={"access_token": "nope"})).status == 401


async def test_workflow_routes_require_token(client: TestClient) -> None:
    resp = await client.get("/api/workflow/v1/projects/PRJ-0001/runs/run-1")
    assert resp.status == 401


async def test_unmatched_routes_do_not_leak_existence_without_token(client: TestClient) -> None:
    assert (await client.get("/api/does-not-exist")).status == 401


@pytest.mark.parametrize("path", ["/health", "/version", "/api/health", "/api/version"])
async def test_probe_routes_stay_open(client: TestClient, path: str) -> None:
    resp = await client.get(path)
    assert resp.status == 200


async def test_probe_paths_are_only_exempt_for_reads(client: TestClient) -> None:
    resp = await client.post("/api/version")
    assert resp.status == 401


async def test_cors_preflight_needs_no_token(client: TestClient) -> None:
    resp = await client.options("/api/config", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]


async def test_unauthorized_response_keeps_cors_headers(client: TestClient) -> None:
    resp = await client.get("/api/projects", headers={"Origin": "http://localhost:5173"})
    assert resp.status == 401
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


# ── Origin policy ────────────────────────────────────────────────────


async def test_foreign_origin_is_rejected_even_with_token(client: TestClient) -> None:
    resp = await client.post("/api/projects", headers={**AUTH, "Origin": "https://evil.example"}, json={})
    assert resp.status == 403
    assert "Access-Control-Allow-Origin" not in resp.headers


async def test_requests_without_origin_are_allowed(client: TestClient) -> None:
    resp = await client.get("/api/projects", headers=AUTH)
    assert resp.status == 200


@pytest.mark.parametrize("origin", DEFAULT_ALLOWED_ORIGINS)
async def test_default_origins_are_allowed(client: TestClient, origin: str) -> None:
    resp = await client.get("/api/projects", headers={**AUTH, "Origin": origin})
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == origin


async def test_explicit_wildcard_origin_is_still_honored(channel: UiChannel, client: TestClient) -> None:
    channel.config.cors_origins = ["*"]
    resp = await client.get("/api/projects", headers={**AUTH, "Origin": "https://any.example"})
    assert resp.status == 200


async def test_env_extends_allowed_origins(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv(gateway_auth.ALLOWED_ORIGINS_ENV, "http://mira.lan:8080, https://mira.example")
    resp = await client.get("/api/projects", headers={**AUTH, "Origin": "http://mira.lan:8080"})
    assert resp.status == 200
    resp = await client.get("/api/projects", headers={**AUTH, "Origin": "http://localhost:5173"})
    assert resp.status == 200


def test_ui_config_default_origins_are_not_wildcard() -> None:
    assert UiChannelConfig().cors_origins == list(DEFAULT_ALLOWED_ORIGINS)
    assert "*" not in UiChannelConfig().cors_origins


# ── WebSocket handshake ──────────────────────────────────────────────


async def test_ws_rejects_missing_token_before_upgrade(client: TestClient) -> None:
    with pytest.raises(WSServerHandshakeError) as exc:
        await client.ws_connect("/ws")
    assert exc.value.status == 401


async def test_ws_rejects_wrong_token(client: TestClient) -> None:
    with pytest.raises(WSServerHandshakeError) as exc:
        await client.ws_connect("/ws", params={"access_token": "wrong"})
    assert exc.value.status == 401


async def test_ws_rejects_foreign_origin(client: TestClient) -> None:
    with pytest.raises(WSServerHandshakeError) as exc:
        await client.ws_connect("/ws", params={"access_token": TOKEN},
                                headers={"Origin": "https://evil.example"})
    assert exc.value.status == 403


async def test_ws_accepts_token_from_electron_origin(client: TestClient) -> None:
    ws = await client.ws_connect("/ws", params={"access_token": TOKEN}, headers={"Origin": "file://"})
    await ws.send_str("not json")
    reply = await ws.receive_json(timeout=5)
    assert reply == {"type": "error", "content": "Invalid JSON"}
    await ws.close()


# ── Bind and logging ─────────────────────────────────────────────────


def test_gateway_binds_loopback_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert GatewayConfig().host == "127.0.0.1"
    assert Config().gateway.host == "127.0.0.1"
    assert _make_channel(tmp_path, monkeypatch).bind_host == "127.0.0.1"


def test_non_loopback_bind_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    # Capture directly: other tests may leave the mira_engine loguru namespace disabled.
    messages: list[str] = []
    monkeypatch.setattr(gateway_auth, "logger", SimpleNamespace(
        warning=lambda message, *args: messages.append(message.format(*args))))
    gateway_auth.warn_if_exposed("0.0.0.0", DEFAULT_ALLOWED_ORIGINS)
    gateway_auth.warn_if_exposed("127.0.0.1", ["*"])
    gateway_auth.warn_if_exposed("::1", DEFAULT_ALLOWED_ORIGINS)
    gateway_auth.warn_if_exposed("localhost", DEFAULT_ALLOWED_ORIGINS)
    assert len(messages) == 2
    assert "non-loopback" in messages[0]
    assert "'*'" in messages[1]


async def test_started_gateway_never_logs_query_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv(gateway_auth.TOKEN_ENV, raising=False)
    channel = _make_channel(tmp_path, monkeypatch, auth_token=TOKEN, bind_port=0)
    monkeypatch.setattr(channel, "_free_port_from_stale_engine", lambda: None)
    events: list[str] = []
    from mira_engine import observability

    real_emit = observability.emit_event
    monkeypatch.setattr(observability, "emit_event",
                        lambda *a, **k: events.append(repr((a, k))) or real_emit(*a, **k))
    loguru_lines: list[str] = []
    sink = logger.add(lambda message: loguru_lines.append(str(message)), level="DEBUG")
    caplog.set_level(logging.DEBUG)  # what `mira gateway -v` enables for stdlib loggers
    task = asyncio.create_task(channel.start())
    try:
        for _ in range(100):
            if channel._running:
                break
            await asyncio.sleep(0.02)
        port = channel._site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/api/projects",
                                   params={"access_token": TOKEN}) as resp:
                assert resp.status == 200
            async with session.get(f"http://127.0.0.1:{port}/api/projects",
                                   params={"access_token": TOKEN + "x"}) as resp:
                assert resp.status == 401
    finally:
        await channel.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.remove(sink)
    assert channel.bind_host == "127.0.0.1"
    assert any("http.completed" in event for event in events)
    captured = "\n".join([caplog.text, *loguru_lines, *events])
    assert TOKEN not in captured


# ── Token storage and resolution ─────────────────────────────────────


def test_token_file_is_created_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "home" / "runtime" / "gateway-token"
    first = ensure_token_file(path)
    assert len(first) >= 32
    assert ensure_token_file(path) == first
    assert path.read_text(encoding="utf-8").strip() == first
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["gateway-token"]


def test_token_file_rotation_and_corrupt_file_recovery(tmp_path: Path) -> None:
    path = tmp_path / "gateway-token"
    first = ensure_token_file(path)
    rotated = ensure_token_file(path, rotate=True)
    assert rotated != first
    assert ensure_token_file(path) == rotated
    path.write_text("\n", encoding="utf-8")
    recovered = ensure_token_file(path)
    assert recovered and recovered != rotated


def test_concurrent_creators_agree_on_one_token(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "gateway-token"
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = set(pool.map(lambda _: ensure_token_file(path), range(16)))
    assert len(tokens) == 1


def test_token_precedence_env_then_config_then_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(gateway_auth.TOKEN_ENV, raising=False)
    file_token = resolve_gateway_token("")
    assert gateway_token_path().read_text(encoding="utf-8").strip() == file_token
    assert resolve_gateway_token("from-config") == "from-config"
    monkeypatch.setenv(gateway_auth.TOKEN_ENV, "from-env")
    assert resolve_gateway_token("from-config") == "from-env"


def test_gateway_token_path_lives_under_mira_home() -> None:
    assert gateway_token_path() == Path(os.environ["MIRA_HOME"]) / "runtime" / "gateway-token"


async def test_channel_resolves_token_file_when_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(gateway_auth.TOKEN_ENV, raising=False)
    channel = _make_channel(tmp_path, monkeypatch)
    test_client = TestClient(TestServer(channel._build_app()))
    await test_client.start_server()
    try:
        token = gateway_token_path().read_text(encoding="utf-8").strip()
        ok = await test_client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})
        assert ok.status == 200
    finally:
        await test_client.close()


def test_cli_prints_and_rotates_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from mira_engine.cli.commands import app

    monkeypatch.delenv(gateway_auth.TOKEN_ENV, raising=False)
    runner = CliRunner()
    first = runner.invoke(app, ["gateway-token"])
    assert first.exit_code == 0, first.output
    token = first.stdout.strip()
    assert token == gateway_token_path().read_text(encoding="utf-8").strip()
    assert runner.invoke(app, ["gateway-token"]).stdout.strip() == token
    rotated = runner.invoke(app, ["gateway-token", "--rotate"])
    assert rotated.exit_code == 0
    assert rotated.stdout.strip() not in {"", token}


# ── Local clients (service manager) ──────────────────────────────────


def test_local_gateway_tokens_cover_custom_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(gateway_auth.TOKEN_ENV, raising=False)
    config = tmp_path / "elsewhere" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"gateway": {"authToken": "cfg-token"}}), encoding="utf-8")
    home_token = ensure_token_file(gateway_token_path())
    side_token = ensure_token_file(gateway_token_path(config.parent))
    assert local_gateway_tokens(config) == ["cfg-token", home_token, side_token]
    monkeypatch.setenv(gateway_auth.TOKEN_ENV, "env-token")
    assert local_gateway_tokens(config)[0] == "env-token"


def test_service_manager_sends_token_to_config_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    from io import BytesIO

    monkeypatch.delenv(gateway_auth.TOKEN_ENV, raising=False)
    stale = ensure_token_file(gateway_token_path(tmp_path))
    good = ensure_token_file(gateway_token_path())
    seen: list[str | None] = []

    def fake_urlopen(request, timeout=None):
        auth = request.get_header("Authorization")
        seen.append(auth)
        if auth != f"Bearer {good}":
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)
        return BytesIO(json.dumps({"config_path": "/x/config.json"}).encode())

    monkeypatch.setattr(svc.urllib.request, "urlopen", fake_urlopen)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    assert svc._gateway_config_path("http://127.0.0.1:1", str(config)) == "/x/config.json"
    assert seen == [f"Bearer {good}"]
    # A token that the gateway rejects is skipped rather than failing the probe.
    stale_first = [stale, good]
    monkeypatch.setattr(gateway_auth, "local_gateway_tokens", lambda _path=None: stale_first)
    seen.clear()
    assert svc._gateway_config_path("http://127.0.0.1:1", str(config)) == "/x/config.json"
    assert seen == [f"Bearer {stale}", f"Bearer {good}"]


# --- legacy wildcard CORS migration ---------------------------------------------


def _load_ui_origins(tmp_path: Path, ui: dict) -> list[str]:
    from mira_engine.config import loader

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"channels": {"ui": ui}}), encoding="utf-8")
    section = loader.load_config(path).channels.ui
    data = section if isinstance(section, dict) else section.model_dump(by_alias=True)
    return data.get("corsOrigins", data.get("cors_origins"))


@pytest.mark.parametrize("key", ["corsOrigins", "cors_origins"])
def test_legacy_wildcard_cors_is_migrated_to_defaults(tmp_path: Path, key: str) -> None:
    assert _load_ui_origins(tmp_path, {key: ["*"]}) == list(DEFAULT_ALLOWED_ORIGINS)


@pytest.mark.parametrize("origins", [
    ["https://mira.example.com"],
    ["*", "https://mira.example.com"],
    [],
])
def test_non_legacy_cors_lists_are_untouched(tmp_path: Path, origins: list[str]) -> None:
    assert _load_ui_origins(tmp_path, {"corsOrigins": origins}) == origins


def test_cors_migration_is_idempotent_and_logged_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mira_engine.config import loader

    monkeypatch.setattr(loader, "_legacy_cors_migration_logged", False)
    # Other tests toggle loguru namespaces globally; stub the loader's logger instead.
    fake_logger = MagicMock()
    monkeypatch.setattr(loader, "logger", fake_logger)
    first = _load_ui_origins(tmp_path, {"corsOrigins": ["*"]})
    second = _load_ui_origins(tmp_path, {"corsOrigins": first})
    _load_ui_origins(tmp_path, {"corsOrigins": ["*"]})
    assert first == second == list(DEFAULT_ALLOWED_ORIGINS)
    fake_logger.info.assert_called_once()


def test_wildcard_opt_out_via_env_survives_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gateway_auth.ALLOWED_ORIGINS_ENV, "*")
    origins = gateway_auth.allowed_origins(_load_ui_origins(tmp_path, {"corsOrigins": ["*"]}))
    assert "*" in origins
    assert gateway_auth.origin_allowed("https://any.example", origins)
