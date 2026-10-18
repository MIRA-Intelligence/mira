"""Focused tests for the fixed-session OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from mira_engine.api.server import (
    API_CHAT_ID,
    API_SESSION_KEY,
    _chat_completion_response,
    _error_json,
    create_app,
    handle_chat_completions,
)
from mira_engine.projects import ProjectRegistry
from mira_engine.security import api_auth

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

TEST_KEY = "test-key"
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


def _make_mock_agent(response_text: str = "mock response") -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value=response_text)
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()
    return agent


@pytest.fixture
def mock_agent():
    return _make_mock_agent()


@pytest.fixture
def app(mock_agent):
    return create_app(
        mock_agent, model_name="test-model", request_timeout=10.0, api_keys=[TEST_KEY],
    )


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app, headers: dict[str, str] | None = AUTH):
        client = TestClient(TestServer(app), headers=headers)
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


def test_error_json() -> None:
    resp = _error_json(400, "bad request")
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"]["message"] == "bad request"
    assert body["error"]["code"] == 400


def test_chat_completion_response() -> None:
    result = _chat_completion_response("hello world", "test-model")
    assert result["object"] == "chat.completion"
    assert result["model"] == "test-model"
    assert result["choices"][0]["message"]["content"] == "hello world"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["id"].startswith("chatcmpl-")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_missing_messages_returns_400(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post("/v1/chat/completions", json={"model": "test"})
    assert resp.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_no_user_message_returns_400(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "system", "content": "you are a bot"}]},
    )
    assert resp.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_true_returns_400(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}], "stream": True},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "stream" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_model_mismatch_returns_400() -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "model": "other-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    request.app = {
        "agent_loop": _make_mock_agent(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_lock": asyncio.Lock(),
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "test-model" in body["error"]["message"]


@pytest.mark.asyncio
async def test_single_user_message_required() -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "previous reply"},
            ],
        }
    )
    request.app = {
        "agent_loop": _make_mock_agent(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_lock": asyncio.Lock(),
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "single user message" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_single_user_message_must_have_user_role() -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "messages": [{"role": "system", "content": "you are a bot"}],
        }
    )
    request.app = {
        "agent_loop": _make_mock_agent(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_lock": asyncio.Lock(),
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "single user message" in body["error"]["message"].lower()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_successful_request_uses_fixed_api_session(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="test-model", api_keys=[TEST_KEY])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == "mock response"
    assert body["model"] == "test-model"
    mock_agent.process_direct.assert_called_once_with(
        content="hello",
        session_key=API_SESSION_KEY,
        channel="api",
        chat_id=API_CHAT_ID,
    )


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_project_dir_requires_registered_project_id(aiohttp_client, mock_agent, tmp_path) -> None:
    app = create_app(mock_agent, model_name="m", api_keys=[TEST_KEY])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "project_dir": str(tmp_path / "outside"),
        },
    )

    assert resp.status == 400
    body = await resp.json()
    assert "project_id is required" in body["error"]["message"]
    mock_agent.process_direct.assert_not_called()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_registered_project_metadata_is_forwarded(aiohttp_client, mock_agent, tmp_path) -> None:
    registry = ProjectRegistry(
        tmp_path / "projects",
        workspace_path=tmp_path / "workspace.json",
        legacy_index_path=tmp_path / "project-dirs.json",
    )
    ref = registry.create_project(project_id="alpha", display_name="Alpha")
    app = create_app(mock_agent, model_name="m", project_registry=registry, api_keys=[TEST_KEY])
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "project_id": "alpha",
            "project_dir": str(ref.project_dir),
        },
    )

    assert resp.status == 200
    mock_agent.process_direct.assert_called_once_with(
        content="hello",
        session_key=API_SESSION_KEY,
        channel="api",
        chat_id=API_CHAT_ID,
        metadata={"project_id": "alpha", "project_dir": str(ref.project_dir)},
    )


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_project_dir_must_match_registry(aiohttp_client, mock_agent, tmp_path) -> None:
    registry = ProjectRegistry(
        tmp_path / "projects",
        workspace_path=tmp_path / "workspace.json",
        legacy_index_path=tmp_path / "project-dirs.json",
    )
    registry.create_project(project_id="alpha", display_name="Alpha")
    app = create_app(mock_agent, model_name="m", project_registry=registry, api_keys=[TEST_KEY])
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "project_id": "alpha",
            "project_dir": str(tmp_path / "outside"),
        },
    )

    assert resp.status == 400
    body = await resp.json()
    assert "does not match" in body["error"]["message"]
    mock_agent.process_direct.assert_not_called()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_followup_requests_share_same_session_key(aiohttp_client) -> None:
    call_log: list[str] = []

    async def fake_process(content, session_key="", channel="", chat_id=""):
        call_log.append(session_key)
        return f"reply to {content}"

    agent = MagicMock()
    agent.process_direct = fake_process
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m", api_keys=[TEST_KEY])
    client = await aiohttp_client(app)

    r1 = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    r2 = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "second"}]},
    )

    assert r1.status == 200
    assert r2.status == 200
    assert call_log == [API_SESSION_KEY, API_SESSION_KEY]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_fixed_session_requests_are_serialized(aiohttp_client) -> None:
    order: list[str] = []

    async def slow_process(content, session_key="", channel="", chat_id=""):
        order.append(f"start:{content}")
        await asyncio.sleep(0.1)
        order.append(f"end:{content}")
        return content

    agent = MagicMock()
    agent.process_direct = slow_process
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m", api_keys=[TEST_KEY])
    client = await aiohttp_client(app)

    async def send(msg: str):
        return await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": msg}]},
        )

    r1, r2 = await asyncio.gather(send("first"), send("second"))
    assert r1.status == 200
    assert r2.status == 200
    # Verify serialization: one process must fully finish before the other starts
    if order[0] == "start:first":
        assert order.index("end:first") < order.index("start:second")
    else:
        assert order.index("end:second") < order.index("start:first")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_models_endpoint(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.get("/v1/models")
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "test-model"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_health_endpoint(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_multimodal_content_extracts_text(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="m", api_keys=[TEST_KEY])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ]
        },
    )
    assert resp.status == 200
    mock_agent.process_direct.assert_called_once_with(
        content="describe this",
        session_key=API_SESSION_KEY,
        channel="api",
        chat_id=API_CHAT_ID,
    )


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_empty_response_retry_then_success(aiohttp_client) -> None:
    call_count = 0

    async def sometimes_empty(content, session_key="", channel="", chat_id=""):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ""
        return "recovered response"

    agent = MagicMock()
    agent.process_direct = sometimes_empty
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m", api_keys=[TEST_KEY])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == "recovered response"
    assert call_count == 2


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_empty_response_falls_back(aiohttp_client) -> None:
    from mira_engine.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

    call_count = 0

    async def always_empty(content, session_key="", channel="", chat_id=""):
        nonlocal call_count
        call_count += 1
        return ""

    agent = MagicMock()
    agent.process_direct = always_empty
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m", api_keys=[TEST_KEY])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == EMPTY_FINAL_RESPONSE_MESSAGE
    assert call_count == 2


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def test_create_app_requires_a_key(mock_agent) -> None:
    with pytest.raises(ValueError):
        create_app(mock_agent, api_keys=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [None, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TEST_KEY}"}],
)
@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/v1/chat/completions"), ("GET", "/v1/models")],
)
async def test_v1_requires_valid_key(aiohttp_client, app, headers, method, path) -> None:
    client = await aiohttp_client(app, headers=headers)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    resp = await client.request(method, path, json=body)
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    error = (await resp.json())["error"]
    assert error["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_query_string_key_is_not_accepted(aiohttp_client, app) -> None:
    client = await aiohttp_client(app, headers=None)
    resp = await client.get(f"/v1/models?access_token={TEST_KEY}")
    assert resp.status == 401


@pytest.mark.asyncio
async def test_any_configured_key_is_accepted(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, api_keys=["first", "second"])
    client = await aiohttp_client(app, headers={"Authorization": "Bearer second"})
    assert (await client.get("/v1/models")).status == 200


@pytest.mark.asyncio
async def test_health_needs_no_key(aiohttp_client, app) -> None:
    client = await aiohttp_client(app, headers={"Origin": "https://evil.example"})
    resp = await client.get("/health")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_browser_origin_rejected_by_default(aiohttp_client, app, mock_agent) -> None:
    client = await aiohttp_client(app, headers={**AUTH, "Origin": "https://evil.example"})
    resp = await client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status == 403
    assert "Access-Control-Allow-Origin" not in resp.headers
    mock_agent.process_direct.assert_not_called()

    preflight = await client.options(
        "/v1/chat/completions",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status == 403
    assert "Access-Control-Allow-Origin" not in preflight.headers


@pytest.mark.asyncio
async def test_allowlisted_origin_gets_cors(aiohttp_client, mock_agent) -> None:
    origin = "http://localhost:3000"
    app = create_app(mock_agent, api_keys=[TEST_KEY], allowed_origins=[origin])
    client = await aiohttp_client(app, headers={"Origin": origin})

    preflight = await client.options(
        "/v1/chat/completions",
        headers={"Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "authorization, content-type"},
    )
    assert preflight.status == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == origin
    assert "Authorization" in preflight.headers["Access-Control-Allow-Headers"]

    unauthorized = await client.get("/v1/models")
    assert unauthorized.status == 401
    assert unauthorized.headers["Access-Control-Allow-Origin"] == origin

    ok = await client.get("/v1/models", headers=AUTH)
    assert ok.status == 200
    assert ok.headers["Access-Control-Allow-Origin"] == origin


def test_resolve_api_keys_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(api_auth.API_KEYS_ENV, " env-a, env-b ,env-a")
    assert api_auth.resolve_api_keys(["cfg"]) == (["env-a", "env-b"], api_auth.API_KEYS_ENV)

    monkeypatch.delenv(api_auth.API_KEYS_ENV)
    assert api_auth.resolve_api_keys([" cfg ", ""]) == (["cfg"], "api.keys")

    monkeypatch.setenv("MIRA_GATEWAY_TOKEN", "gw-token")
    assert api_auth.resolve_api_keys([]) == (["gw-token"], "gateway token")


def test_api_allowed_origins_has_no_defaults(monkeypatch) -> None:
    monkeypatch.delenv(api_auth.API_ALLOWED_ORIGINS_ENV, raising=False)
    assert api_auth.api_allowed_origins([]) == []
    monkeypatch.setenv(api_auth.API_ALLOWED_ORIGINS_ENV, "http://a, http://b")
    assert api_auth.api_allowed_origins(["http://a"]) == ["http://a", "http://b"]
