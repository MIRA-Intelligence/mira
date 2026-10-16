"""Team settings: administrators' config scope, the push to engines and the LLM proxy."""

from __future__ import annotations

import asyncio
import json
import stat
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.server.conftest import Harness

TEAM_KEY = "sk-team-secret-value"


class FakeProvider:
    """An OpenAI-compatible upstream that records what Mira Server sends it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.server: TestServer | None = None

    async def start(self) -> str:
        async def record(request: web.Request) -> dict[str, Any]:
            entry = {"method": request.method, "path": request.path, "query": request.rel_url.raw_query_string,
                     "headers": dict(request.headers), "body": await request.read()}
            self.requests.append(entry)
            return entry

        async def models(request: web.Request) -> web.Response:
            await record(request)
            return web.json_response({"data": [{"id": "team-model-a"}, {"id": "team-model-b"}]})

        async def chat(request: web.Request) -> web.StreamResponse:
            await record(request)
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for i in range(3):
                await resp.write(f"data: {i}\n\n".encode())
                await asyncio.sleep(0.01)
            await resp.write_eof()
            return resp

        app = web.Application()
        app.router.add_get("/v1/models", models)
        app.router.add_post("/v1/chat/completions", chat)
        app.router.add_post("/v1/messages", chat)
        self.server = TestServer(app)
        await self.server.start_server()
        return f"http://127.0.0.1:{self.server.port}/v1"

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()


@pytest.fixture
async def provider():
    fake = FakeProvider()
    yield fake
    await fake.close()


@pytest.fixture(autouse=True)
def _no_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mira Server's model fetch honours proxy settings (also macOS system ones,
    # unless NO_PROXY is set); the fake upstream is local.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


async def _team_config(harness: Harness, tokens: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict]:
    resp = await harness.client.post("/api/config", json=body, headers=harness.bearer(tokens))
    return resp.status, await resp.json()


async def _set_team_provider(harness: Harness, admin: dict[str, Any], base: str) -> None:
    status, data = await _team_config(harness, admin, {"providers": {"custom": {"api_key": TEAM_KEY,
                                                                                "api_base": base}}})
    assert status == 200, data


# ── roles ────────────────────────────────────────────────────────────


async def test_admin_config_is_the_team_scope_and_starts_no_engine(harness: Harness) -> None:
    admin = await harness.login("bob")
    resp = await harness.client.get("/api/config", headers=harness.bearer(admin))
    assert resp.status == 200
    data = await resp.json()
    assert data["scope"] == "team" and "custom" in data["providers"]
    assert harness.spawner.starts == []


async def test_users_config_goes_to_their_engine(harness: Harness) -> None:
    alice = await harness.login("alice")
    await harness.wait_running(alice)
    resp = await harness.client.get("/api/config", headers=harness.bearer(alice))
    assert resp.status == 404  # the fake engine has no /api/config: the request reached it
    assert harness.spawner.logs["alice"].requests[-1]["path"] == "/api/config"


async def test_admin_with_user_role_still_uses_their_engine_for_everything_else(harness: Harness) -> None:
    admin = await harness.login("bob")
    await harness.wait_running(admin)
    resp = await harness.client.get("/api/echo", headers=harness.bearer(admin))
    assert resp.status == 200 and (await resp.json())["path"] == "/api/echo"


async def test_admin_only_account_signs_in_but_has_no_engine(harness: Harness) -> None:
    dave = await harness.login("dave")
    assert dave["user"]["roles"] == ["admin"]
    headers = harness.bearer(dave)
    assert (await harness.client.get("/server/me", headers=headers)).status == 200
    for method, path in (("GET", "/api/echo"), ("POST", "/server/engine/start"), ("GET", "/ws")):
        resp = await harness.client.request(method, path, headers=headers)
        assert resp.status == 403, path
        assert (await resp.json())["error"] == "no_engine"
    assert (await harness.client.get("/api/config", headers=headers)).status == 200
    assert harness.spawner.starts == []


async def test_leaving_the_users_group_stops_an_admins_engine(harness_factory) -> None:
    harness = await harness_factory(auth={"group_recheck_interval": 0})
    admin = await harness.login("bob")
    await harness.wait_running(admin)
    bob = harness.resolver.users["bob"]
    harness.resolver.users["bob"] = replace(bob, roles=("admin",))
    resp = await harness.client.get("/server/me", headers=harness.bearer(admin))
    assert resp.status == 200  # still an administrator
    await harness.server.engines.drain()
    assert harness.spawner.stops == ["bob"]
    assert harness.store.audit_events("engine.revoked")


async def test_clients_cannot_push_a_team_layer(harness: Harness) -> None:
    for user in ("alice", "bob"):
        tokens = await harness.login(user)
        await harness.wait_running(tokens)
        resp = await harness.client.put("/api/team/layer", json={"layer": None}, headers=harness.bearer(tokens))
        assert resp.status == 404


# ── changing the team settings ───────────────────────────────────────


async def test_team_key_is_stored_privately_masked_and_audited_without_value(harness: Harness) -> None:
    admin = await harness.login("bob")
    status, data = await _team_config(harness, admin, {"providers": {"custom": {
        "api_key": TEAM_KEY, "api_base": "https://llm.example.com/v1"}}})
    assert status == 200 and data["persisted"] is True and data["scope"] == "team"
    assert data["providers"]["custom"]["api_key_preview"] == "sk-t...ue"
    assert TEAM_KEY not in json.dumps(data)

    path = harness.server.team.path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["providers"]["custom"]["apiKey"] == TEAM_KEY

    events = harness.store.audit_events("team.config_changed")
    assert events[-1]["username"] == "bob"
    assert events[-1]["detail"]["settings"] == ["providers.custom.apiBase", "providers.custom.apiKey"]
    assert TEAM_KEY not in json.dumps(harness.store.audit_events())


async def test_unchanged_save_is_not_audited(harness: Harness) -> None:
    admin = await harness.login("bob")
    current = await (await harness.client.get("/api/config", headers=harness.bearer(admin))).json()
    status, data = await _team_config(harness, admin, {"runtime": {
        "workspace": current["runtime"]["workspace"], "restrict_to_workspace": False,
        "max_tool_iterations": current["runtime"]["max_tool_iterations"]}})
    assert status == 200 and data["persisted"] is False
    assert harness.store.audit_events("team.config_changed") == []


@pytest.mark.parametrize("body, message", [
    ({"projects_root": "/elsewhere"}, "per user"),
    ({"runtime": {"workspace": "/elsewhere"}}, "per user"),
    ({"runtime": {"restrict_to_workspace": True}}, "host policy"),
    ({"providers": {"openai_codex": {"api_key": "x"}}}, "signs in per user"),
    ({"providers": {"github_copilot": {"api_base": "https://example.com"}}}, "signs in per user"),
    ({"providers": {"gemini": {"api_key": "x"}}}, "API Base"),
    ({"runtime": {"temperature": 7}}, "temperature"),
])
async def test_invalid_team_changes_are_400(harness: Harness, body: dict, message: str) -> None:
    admin = await harness.login("bob")
    status, data = await _team_config(harness, admin, body)
    assert status == 400 and message in data["error"]
    assert not harness.server.team.path.exists()


async def test_named_custom_team_provider_reaches_engines_and_the_proxy(harness: Harness,
                                                                         provider: FakeProvider) -> None:
    base = await provider.start()
    alice = await harness.login("alice")
    await harness.wait_running(alice)
    admin = await harness.login("bob")
    status, data = await _team_config(harness, admin, {"providers": {"custom_corp": {
        "name": "Corp LLM", "api_key": TEAM_KEY, "api_base": base, "models": ["corp-model"]}}})
    assert status == 200 and data["providers"]["custom_corp"]["api_key_preview"] == "sk-t...ue", data
    await harness.server.engines.drain()
    layer = harness.spawner.logs["alice"].team_layers[-1]
    token = harness.spawner.tokens["alice"]
    assert layer["providers"]["custom_corp"] == {
        "api_key": token, "api_base": f"{harness.server.llm_proxy_base}/custom_corp",
        "models": ["corp-model"], "enabled": None, "name": "Corp LLM"}
    resp = await harness.client.post("/llm/custom_corp/chat/completions", data=b"{}",
                                     headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 200 and provider.requests[-1]["headers"]["Authorization"] == f"Bearer {TEAM_KEY}"


async def test_admin_turns_providers_off_for_everyone(harness: Harness) -> None:
    alice = await harness.login("alice")
    await harness.wait_running(alice)
    admin = await harness.login("bob")
    # What the Providers page sends when an OAuth provider and a plain one are switched off.
    status, data = await _team_config(harness, admin, {"providers": {
        "openai_codex": {"api_base": None, "models": [], "enabled": False},
        "deepseek": {"api_base": None, "models": [], "enabled": False},
    }})
    assert status == 200 and data["persisted"] is True, data
    assert data["providers"]["openai_codex"]["enabled"] is False
    assert (await (await harness.client.get("/api/config", headers=harness.bearer(admin))).json()) \
        ["providers"]["deepseek"]["enabled"] is False  # kept for the next visit
    await harness.server.engines.drain()
    layer = harness.spawner.logs["alice"].team_layers[-1]
    assert layer["disabled"] == ["deepseek", "openai_codex"] and layer["providers"] == {}


async def test_admin_tests_and_lists_models_with_the_team_key(harness: Harness, provider: FakeProvider) -> None:
    base = await provider.start()
    admin = await harness.login("bob")
    await _set_team_provider(harness, admin, base)
    resp = await harness.client.post("/api/providers/custom/test", json={}, headers=harness.bearer(admin))
    assert (await resp.json()) == {"ok": True, "message": "Connection succeeded", "model_count": 2}
    assert provider.requests[-1]["headers"]["Authorization"] == f"Bearer {TEAM_KEY}"

    resp = await harness.client.get("/api/providers/custom/models?refresh=1", headers=harness.bearer(admin))
    data = await resp.json()
    assert resp.status == 200 and data["models"] == ["team-model-a", "team-model-b"] and data["cached"] is False
    resp = await harness.client.get("/api/providers/custom/models", headers=harness.bearer(admin))
    assert (await resp.json())["cached"] is True
    assert harness.spawner.starts == []


# ── the layer engines get ────────────────────────────────────────────


async def test_engines_get_the_layer_on_start_and_on_every_change(harness: Harness) -> None:
    alice = await harness.login("alice")
    await harness.wait_running(alice)
    first = harness.spawner.logs["alice"].team_layers[-1]
    assert first["providers"] == {}

    admin = await harness.login("bob")
    await _set_team_provider(harness, admin, "https://llm.example.com/v1")
    status, _ = await _team_config(harness, admin, {"runtime": {"provider": "custom", "model": "team-model-a"}})
    assert status == 200
    await harness.server.engines.drain()

    layer = harness.spawner.logs["alice"].team_layers[-1]
    token = harness.spawner.tokens["alice"]
    assert layer["providers"]["custom"]["api_key"] == token
    assert layer["providers"]["custom"]["api_base"] == f"{harness.server.llm_proxy_base}/custom"
    assert layer["defaults"] == {"provider": "custom", "model": "team-model-a"}
    assert layer["revision"] != first["revision"]
    assert TEAM_KEY not in json.dumps(harness.spawner.logs["alice"].team_layers)


async def test_llm_proxy_base_is_how_engines_reach_mira_server(harness_factory) -> None:
    for listen, expected in (("127.0.0.1:8740", "http://127.0.0.1:8740/llm"),
                             ("0.0.0.0:9000", "http://127.0.0.1:9000/llm"),
                             ("[::1]:8740", "http://[::1]:8740/llm")):
        harness = await harness_factory(server={"listen": listen})
        assert harness.server.llm_proxy_base == expected


# ── the LLM proxy ────────────────────────────────────────────────────


async def _proxy_setup(harness: Harness, provider: FakeProvider) -> str:
    base = await provider.start()
    admin = await harness.login("bob")
    await _set_team_provider(harness, admin, base)
    alice = await harness.login("alice")
    await harness.wait_running(alice)
    return harness.spawner.tokens["alice"]


async def test_llm_proxy_swaps_the_engine_token_for_the_team_key(harness: Harness, provider: FakeProvider) -> None:
    token = await _proxy_setup(harness, provider)
    resp = await harness.client.post("/llm/custom/chat/completions?stream=1", data=b'{"model": "m"}',
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
    assert resp.status == 200
    assert await resp.read() == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"
    sent = provider.requests[-1]
    assert sent["path"] == "/v1/chat/completions" and sent["query"] == "stream=1"
    assert sent["headers"]["Authorization"] == f"Bearer {TEAM_KEY}"
    assert sent["body"] == b'{"model": "m"}'
    assert sent["headers"]["Content-Length"] == "14" and "Transfer-Encoding" not in sent["headers"]
    assert token not in json.dumps(sent, default=str)


@pytest.mark.parametrize("header", ["x-api-key", "api-key"])
async def test_llm_proxy_swaps_other_key_headers(harness: Harness, provider: FakeProvider, header: str) -> None:
    token = await _proxy_setup(harness, provider)
    resp = await harness.client.post("/llm/custom/messages", data=b"{}", headers={header: token})
    assert resp.status == 200
    assert provider.requests[-1]["headers"][header] == TEAM_KEY


async def test_llm_proxy_swaps_a_query_key(harness: Harness, provider: FakeProvider) -> None:
    token = await _proxy_setup(harness, provider)
    resp = await harness.client.get(f"/llm/custom/models?key={token}&alt=json")
    assert resp.status == 200
    assert provider.requests[-1]["query"] == f"alt=json&key={TEAM_KEY}"


@pytest.mark.parametrize("headers, status", [
    ({}, 401),
    ({"Authorization": "Bearer not-an-engine-token"}, 401),
    ({"Authorization": "Basic abc"}, 401),
])
async def test_llm_proxy_needs_a_running_engines_token(harness: Harness, provider: FakeProvider,
                                                       headers: dict, status: int) -> None:
    await _proxy_setup(harness, provider)
    resp = await harness.client.post("/llm/custom/chat/completions", data=b"{}", headers=headers)
    assert resp.status == status
    assert provider.requests == [] or provider.requests[-1]["path"] != "/v1/chat/completions"


async def test_llm_proxy_refuses_requests_through_the_tls_proxy(harness: Harness, provider: FakeProvider) -> None:
    token = await _proxy_setup(harness, provider)
    resp = await harness.client.post("/llm/custom/chat/completions", data=b"{}",
                                     headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "10.0.0.9"})
    assert resp.status == 403


async def test_llm_proxy_only_serves_team_providers(harness: Harness, provider: FakeProvider) -> None:
    token = await _proxy_setup(harness, provider)
    resp = await harness.client.post("/llm/openai/chat/completions", data=b"{}",
                                     headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 404


async def test_llm_proxy_stops_serving_a_removed_user(harness_factory, provider: FakeProvider) -> None:
    harness = await harness_factory(auth={"group_recheck_interval": 0})
    token = await _proxy_setup(harness, provider)
    del harness.resolver.users["alice"]
    harness.resolver.outsiders.add("alice")
    resp = await harness.client.post("/llm/custom/chat/completions", data=b"{}",
                                     headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 403
