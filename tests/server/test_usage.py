"""Token usage of team providers and per-user token quotas."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from typer.testing import CliRunner

from mira_server.cli import app
from mira_server.store import Store
from mira_server.usage import (
    Limits,
    QuotaError,
    TokenCounts,
    UsageMeter,
    local_day,
    parse_quotas,
    period_reset,
    request_model,
)
from tests.server.conftest import Harness
from tests.server.test_team import TEAM_KEY, _no_http_proxy, _set_team_provider  # noqa: F401


def _meter(content_type: str, *chunks: bytes) -> TokenCounts | None:
    meter = UsageMeter(content_type)
    for chunk in chunks:
        meter.feed(chunk)
    return meter.result()


def _sse(*events: dict[str, Any], done: bool = False) -> bytes:
    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


# ── reading usage from responses ─────────────────────────────────────


def test_openai_json_response() -> None:
    body = json.dumps({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                                                 "prompt_tokens_details": {"cached_tokens": 64}}}).encode()
    assert _meter("application/json", body[:10], body[10:]) == TokenCounts(100, 20, 64, 120)


def test_openai_stream_with_the_usage_chunk_split_across_reads() -> None:
    data = _sse({"choices": [{"delta": {"content": "hi"}}], "usage": None},
                {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}, done=True)
    assert _meter("text/event-stream; charset=utf-8", data[:25], data[25:61], data[61:]) == TokenCounts(7, 3, 0, 10)


def test_responses_api_stream() -> None:
    data = _sse({"type": "response.output_text.delta", "delta": "x"},
                {"type": "response.completed", "response": {"usage": {
                    "input_tokens": 50, "output_tokens": 5, "total_tokens": 55,
                    "input_tokens_details": {"cached_tokens": 10}}}})
    assert _meter("text/event-stream", data) == TokenCounts(50, 5, 10, 55)


def test_anthropic_stream_counts_cache_reads_and_writes_as_input() -> None:
    data = (b"event: message_start\n" + _sse({"type": "message_start", "message": {"usage": {
        "input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20, "output_tokens": 1}}})
        + b"event: message_delta\n" + _sse({"type": "message_delta", "usage": {"output_tokens": 42}}))
    assert _meter("text/event-stream", data) == TokenCounts(130, 42, 100, 172)


def test_gemini_stream_and_json_array() -> None:
    usage = {"usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 8, "thoughtsTokenCount": 4,
                               "totalTokenCount": 42, "cachedContentTokenCount": 6}}
    assert _meter("text/event-stream", _sse({"candidates": []}, usage)) == TokenCounts(30, 12, 6, 42)
    assert _meter("application/json", json.dumps([{"candidates": []}, usage]).encode()) == TokenCounts(30, 12, 6, 42)


def test_responses_without_usage_are_unreported() -> None:
    assert _meter("text/event-stream", b"data: 0\n\ndata: 1\n\n") is None
    assert _meter("application/json", b"not json") is None


def test_request_model() -> None:
    assert request_model(b'{"model": "gpt-5", "stream": true}', "/chat/completions") == "gpt-5"
    assert request_model(b"{}", "/models/gemini-2.5-pro:streamGenerateContent") == "gemini-2.5-pro"
    assert request_model(None, "/chat/completions") == ""


# ── quota settings ───────────────────────────────────────────────────


def test_user_limits_follow_the_default_unless_overridden() -> None:
    quotas = parse_quotas({"default": {"daily": 1000, "monthly": 0},
                           "users": {"alice": {"daily": 5000}, "bob": {"daily": 0, "monthly": 70}, "carol": {}}})
    assert quotas.for_user("dave") == Limits(1000, None)  # monthly 0: unlimited
    assert quotas.for_user("alice") == Limits(5000, None)
    assert quotas.for_user("bob") == Limits(None, 70)  # daily 0 for bob: unlimited
    assert "carol" not in quotas.as_json()["users"]  # an empty override is no override
    assert parse_quotas(quotas.as_json()) == quotas


@pytest.mark.parametrize("data, message", [
    ([], "must be an object"),
    ({"default": {"daily": -1}}, "default.daily"),
    ({"default": {"daily": 1.5}}, "default.daily"),
    ({"default": {"weekly": 5}}, "unknown key"),
    ({"users": {"../x": {"daily": 1}}}, "not a valid username"),
    ({"warn_percent": 0}, "warn_percent"),
    ({"limits": {}}, "unknown key"),
])
def test_invalid_quota_settings(data: Any, message: str) -> None:
    with pytest.raises(QuotaError, match=message):
        parse_quotas(data, valid_user=lambda name: name.isalnum())


def test_monthly_reset_crosses_the_year() -> None:
    assert period_reset("monthly", "2026-12-17").startswith("2027-01-01T00:00:00")
    assert period_reset("daily", "2026-02-28").startswith("2026-03-01T00:00:00")


# ── through the LLM proxy ────────────────────────────────────────────


class UsageProvider:
    """An OpenAI-compatible upstream answering with ``tokens`` total tokens per call."""

    def __init__(self, tokens: int = 60) -> None:
        self.tokens = tokens
        self.requests: list[dict[str, Any]] = []
        self.server: TestServer | None = None

    async def start(self) -> str:
        async def chat(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            self.requests.append({"headers": dict(request.headers), "body": body})
            usage = {"prompt_tokens": self.tokens - 10, "completion_tokens": 10, "total_tokens": self.tokens}
            if not body.get("stream"):
                return web.json_response({"choices": [{"message": {"content": "ok"}}], "usage": usage})
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(_sse({"choices": [{"delta": {"content": "ok"}}]}, {"choices": [], "usage": usage},
                                  done=True))
            await resp.write_eof()
            return resp

        async def models(request: web.Request) -> web.Response:
            return web.json_response({"data": [{"id": "m"}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        app.router.add_get("/v1/models", models)
        self.server = TestServer(app)
        await self.server.start_server()
        return f"http://127.0.0.1:{self.server.port}/v1"

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()


@pytest.fixture
async def upstream():
    fake = UsageProvider()
    yield fake
    await fake.close()


async def _setup(harness: Harness, upstream: UsageProvider) -> dict[str, str]:
    base = await upstream.start()
    admin = await harness.login("bob")
    await _set_team_provider(harness, admin, base)
    tokens = {}
    for name in ("alice", "bob"):
        session = admin if name == "bob" else await harness.login(name)
        await harness.wait_running(session)
        tokens[name] = harness.spawner.tokens[name]
    return tokens


async def _call(harness: Harness, token: str, *, model: str = "team-model", stream: bool = True) -> web.Response:
    return await harness.client.post("/llm/custom/chat/completions", json={"model": model, "stream": stream},
                                     headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"})


async def _get(harness: Harness, session: dict[str, Any], path: str) -> tuple[int, Any]:
    resp = await harness.client.get(path, headers=harness.bearer(session))
    return resp.status, (await resp.json() if resp.content_type == "application/json" else await resp.text())


async def _put_quotas(harness: Harness, session: dict[str, Any], body: Any) -> tuple[int, Any]:
    resp = await harness.client.put("/server/admin/quotas", json=body, headers=harness.bearer(session))
    return resp.status, await resp.json()


async def test_the_proxy_meters_each_users_calls(harness: Harness, upstream: UsageProvider) -> None:
    engine_tokens = await _setup(harness, upstream)
    assert (await _call(harness, engine_tokens["alice"])).status == 200
    assert (await _call(harness, engine_tokens["alice"], model="other", stream=False)).status == 200
    assert (await _call(harness, engine_tokens["bob"])).status == 200
    # Sent uncompressed, so the counts can be read; listing models is not a call.
    assert upstream.requests[0]["headers"]["Accept-Encoding"] == "identity"
    await harness.client.get("/llm/custom/models", headers={"Authorization": f"Bearer {engine_tokens['alice']}"})

    alice = await harness.login("alice")
    status, mine = await _get(harness, alice, "/server/usage")
    assert status == 200
    assert mine["today"]["total_tokens"] == 120 and mine["today"]["calls"] == 2
    assert mine["month"]["total_tokens"] == 120
    assert {(row["model"], row["total_tokens"]) for row in mine["by_model"]} == {("team-model", 60), ("other", 60)}
    assert mine["quota"]["daily"]["limit"] is None and mine["quota"]["daily"]["remaining"] is None

    admin = await harness.login("bob")
    status, report = await _get(harness, admin, "/server/admin/usage")
    assert status == 200
    assert {row["user"]: row["total_tokens"] for row in report["rows"]} == {"alice": 120, "bob": 60}
    assert report["totals"]["calls"] == 3 and report["totals"]["unreported_calls"] == 0
    status, by_model = await _get(harness, admin, "/server/admin/usage?group_by=user,model")
    assert [(row["user"], row["model"]) for row in by_model["rows"]] == [
        ("alice", "other"), ("alice", "team-model"), ("bob", "team-model")]
    status, text = await _get(harness, admin, "/server/admin/usage?group_by=user&format=csv")
    assert status == 200 and text.splitlines()[0].startswith("user,input_tokens,output_tokens")
    assert "alice,100,20,0,120,2,0" in text


async def test_usage_reports_are_for_administrators(harness: Harness, upstream: UsageProvider) -> None:
    alice = await harness.login("alice")
    for path in ("/server/admin/usage", "/server/admin/quotas"):
        status, data = await _get(harness, alice, path)
        assert status == 403 and data["error"] == "forbidden"
    status, _ = await _put_quotas(harness, alice, {"default": {"daily": 1}})
    assert status == 403
    status, data = await _get(harness, await harness.login("bob"), "/server/admin/usage?group_by=password")
    assert status == 400


async def test_quota_stops_team_calls_until_the_day_ends(harness: Harness, upstream: UsageProvider) -> None:
    engine_tokens = await _setup(harness, upstream)
    admin = await harness.login("bob")
    status, saved = await _put_quotas(harness, admin, {"default": {"daily": 100, "monthly": None},
                                                       "users": {"bob": {"daily": 0}}})
    assert status == 200 and saved["default"] == {"daily": 100, "monthly": None}
    assert harness.store.audit_events("team.quotas_changed")[-1]["detail"]["users"] == ["bob"]

    assert (await _call(harness, engine_tokens["alice"])).status == 200  # 60 of 100
    assert (await _call(harness, engine_tokens["alice"])).status == 200  # 120: over, but already in flight
    sent = len(upstream.requests)
    refused = await _call(harness, engine_tokens["alice"])
    assert refused.status == 429
    error = (await refused.json())["error"]
    assert error["code"] == "insufficient_quota" and "120 of 100 tokens" in error["message"]
    assert len(upstream.requests) == sent  # never reached the provider
    assert (await _call(harness, engine_tokens["bob"])).status == 200  # bob: daily unlimited

    alice = await harness.login("alice")
    status, mine = await _get(harness, alice, "/server/usage")
    assert mine["quota"]["daily"] | {"resets_at": None} == {
        "limit": 100, "used": 120, "remaining": 0, "warn": True, "exceeded": True, "resets_at": None}
    status, report = await _get(harness, admin, "/server/admin/usage")
    quota = {row["user"]: row["quota"]["daily"]["exceeded"] for row in report["rows"]}
    assert quota == {"alice": True, "bob": False}

    harness.server._wall_clock = lambda: time.time() + 86_400  # the next day
    assert (await _call(harness, engine_tokens["alice"])).status == 200


async def test_monthly_quota_and_per_user_override(harness: Harness, upstream: UsageProvider) -> None:
    engine_tokens = await _setup(harness, upstream)
    admin = await harness.login("bob")
    await _put_quotas(harness, admin, {"default": {"monthly": 50}, "users": {"alice": {"monthly": 1000}}})
    assert (await _call(harness, engine_tokens["bob"])).status == 200
    assert (await _call(harness, engine_tokens["bob"])).status == 429
    assert (await _call(harness, engine_tokens["alice"])).status == 200


async def test_quotas_are_kept_and_invalid_ones_refused(harness: Harness) -> None:
    admin = await harness.login("bob")
    status, data = await _put_quotas(harness, admin, {"default": {"daily": -5}})
    assert status == 400 and "default.daily" in data["message"]
    body = {"default": {"daily": 2_000_000, "monthly": 30_000_000}, "users": {"alice": {"monthly": 0}},
            "warn_percent": 90}
    status, _ = await _put_quotas(harness, admin, body)
    assert status == 200
    assert harness.server._load_quotas().as_json() == {
        "default": {"daily": 2_000_000, "monthly": 30_000_000}, "users": {"alice": {"daily": None, "monthly": 0}},
        "warn_percent": 90}
    status, data = await _get(harness, admin, "/server/admin/quotas")
    assert data["warn_percent"] == 90


async def test_a_call_without_usage_counts_as_unreported(harness: Harness, upstream: UsageProvider,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    engine_tokens = await _setup(harness, upstream)
    monkeypatch.setattr(UsageMeter, "result", lambda self: None)
    assert (await _call(harness, engine_tokens["alice"])).status == 200
    status, mine = await _get(harness, await harness.login("alice"), "/server/usage")
    assert mine["today"]["calls"] == 1 and mine["today"]["unreported_calls"] == 1
    assert mine["today"]["total_tokens"] == 0


# ── command line ─────────────────────────────────────────────────────


def test_cli_quota_and_usage(tmp_path) -> None:
    data_dir = tmp_path / "data"
    config = tmp_path / "mira-server.toml"
    config.write_text(f'[server]\ndata_dir = "{data_dir}"\npublic_url = "https://mira.example.com"\n')
    store = Store(data_dir / "mira-server.sqlite3")
    day = local_day()
    store.record_usage("alice", day, "custom", "m1", TokenCounts(1000, 200, 0, 1200))
    store.record_usage("bob", day, "custom", "m2", None)
    runner = CliRunner()

    result = runner.invoke(app, ["quota", "set", "--daily", "2M", "--monthly", "unlimited", "-c", str(config)])
    assert result.exit_code == 0, result.output
    assert "Default per user: daily 2,000,000, monthly unlimited" in result.output
    result = runner.invoke(app, ["quota", "set", "-u", "alice", "--daily", "500k", "--warn", "90", "-c", str(config)])
    assert result.exit_code == 0 and "alice: daily 500,000, monthly default" in result.output
    assert parse_quotas(store.get_setting("quotas")).for_user("alice") == Limits(500_000, None)
    result = runner.invoke(app, ["quota", "set", "--daily", "default", "-c", str(config)])
    assert result.exit_code != 0  # 'default' is for overrides
    assert store.audit_events("team.quotas_changed")[-1]["detail"]["users"] == ["alice"]

    result = runner.invoke(app, ["usage", "--by", "user,model", "-c", str(config)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].split()[:3] == ["user", "model", "input_tokens"]
    assert lines[2].split() == ["alice", "m1", "1,000", "200", "0", "1,200", "1", "0"]
    assert lines[3].split() == ["bob", "m2", "0", "0", "0", "0", "1", "1"]
    result = runner.invoke(app, ["usage", "--csv", "-c", str(config)])
    assert result.output.splitlines()[1] == "alice,1000,200,0,1200,1,0"
