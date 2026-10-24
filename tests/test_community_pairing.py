"""Device-flow pairing shared by ``mira community login`` and the desktop
app's ``/api/community/pair/*`` handlers (mira_engine.community.pairing).

Until mira#251 each had its own copy on a different HTTP stack (httpx vs
aiohttp) with a different trust store; the CLI could join while the app
could not. One module, one stack, one error type.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mira_engine.community import pairing
from mira_engine.community.client import CommunityError

ROOT = Path(__file__).resolve().parents[1]
_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any test patches it


def _serve(monkeypatch, handler):
    """Every httpx.AsyncClient the engine opens talks to ``handler``."""
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, transport=httpx.MockTransport(handler), **k),
    )


def _answer(monkeypatch, status, body=None, *, text=None):
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content) if request.content else {}))
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body if body is not None else {})

    _serve(monkeypatch, handler)
    return seen


# ------------------------------------------------------------------ start


async def test_start_normalizes_the_answer_and_keeps_the_platforms_body(monkeypatch):
    body = {
        "verification_url": "https://c/pair",
        "user_code": "ABCD",
        "poll_token": "p1",
        "interval": "3",
        "expires_in": 60,
        "extra": 1,
    }
    seen = _answer(monkeypatch, 200, body)
    start = await pairing.start_pairing("https://c/api/", {"domains": ["rag"]})
    assert seen == [("https://c/api/agents/pair/start", {"domains": ["rag"]})]
    assert (start.verification_url, start.user_code, start.poll_token) == ("https://c/pair", "ABCD", "p1")
    assert (start.interval, start.expires_in) == (3, 60)
    assert start.raw == body  # the desktop app passes this on unchanged


async def test_start_accepts_the_device_flow_spelling_and_fills_defaults(monkeypatch):
    _answer(monkeypatch, 200, {"verification_uri": "u", "device_code": "d", "pairing_code": "X"})
    start = await pairing.start_pairing("https://c", {})
    assert (start.verification_url, start.poll_token, start.user_code) == ("u", "d", "X")
    assert (start.interval, start.expires_in) == (5, 300)


@pytest.mark.parametrize(
    ("status", "body", "message", "detail"),
    [
        (404, {}, "pairing endpoint unavailable on the community service", None),
        (400, {"error": "bio too long"}, "pairing failed: bio too long", "bio too long"),
        (503, None, "pairing failed: HTTP 503", None),
        (200, {"user_code": "X"}, "unexpected pairing response from the community service", None),
    ],
)
async def test_start_reports_what_the_platform_said(monkeypatch, status, body, message, detail):
    _answer(monkeypatch, status, body)
    with pytest.raises(pairing.PairingError) as info:
        await pairing.start_pairing("https://c", {})
    assert str(info.value) == message
    assert (info.value.status, info.value.detail, info.value.network) == (status, detail, False)
    assert isinstance(info.value, CommunityError) and info.value.status_code == status


async def test_a_network_failure_is_a_pairing_error_too(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    _serve(monkeypatch, handler)
    with pytest.raises(pairing.PairingError) as info:
        await pairing.start_pairing("https://c", {})
    assert info.value.network and info.value.status is None
    assert str(info.value).startswith("network error: ") and "CERTIFICATE_VERIFY_FAILED" in str(info.value)

    with pytest.raises(pairing.PairingError) as info:  # nothing configured: still an error, not a crash
        await pairing.poll_pairing("", "p")
    assert info.value.network


# ------------------------------------------------------------------ poll


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (202, {}, pairing.PairingPoll("pending")),
        (425, {}, pairing.PairingPoll("pending")),
        (200, {"status": "pending"}, pairing.PairingPoll("pending")),
        (410, {}, pairing.PairingPoll("expired")),
        (200, {"agent_token": "tok", "agent_id": "a1"}, pairing.PairingPoll("authorized", "tok", "a1")),
        (200, {"agent_token": "tok"}, pairing.PairingPoll("authorized", "tok", "")),
    ],
)
async def test_poll_outcomes(monkeypatch, status, body, expected):
    seen = _answer(monkeypatch, status, body)
    assert await pairing.poll_pairing("https://c", "p1") == expected
    assert seen == [("https://c/agents/pair/poll", {"poll_token": "p1"})]


async def test_poll_reports_a_platform_error(monkeypatch):
    _answer(monkeypatch, 500, {"error": "boom"})
    with pytest.raises(pairing.PairingError) as info:
        await pairing.poll_pairing("https://c", "p1")
    assert str(info.value) == "poll failed: HTTP 500"
    assert (info.value.status, info.value.detail) == (500, "boom")


# ------------------------------------------------------------------ me


async def test_fetch_me_is_best_effort(monkeypatch):
    assert await pairing.fetch_me("", "tok") is None
    assert await pairing.fetch_me("https://c", "") is None

    auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth.append(request.headers.get("Authorization"))
        assert request.method == "GET" and str(request.url) == "https://c/agents/me"
        return httpx.Response(200, json={"agent": {"id": "a1", "name": "Nova", "status": "active"}})

    _serve(monkeypatch, handler)
    assert await pairing.fetch_me("https://c/", "tok") == {"id": "a1", "name": "Nova", "status": "active"}
    assert auth == ["Bearer tok"]

    _answer(monkeypatch, 200, {"id": "a1", "name": "Nova"})  # older platform: no envelope
    assert await pairing.fetch_me("https://c", "tok") == {"id": "a1", "name": "Nova"}

    _answer(monkeypatch, 401, {"error": "bad token"})
    assert await pairing.fetch_me("https://c", "tok") is None

    _answer(monkeypatch, 200, text="<html>not json</html>")
    assert await pairing.fetch_me("https://c", "tok") is None

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _serve(monkeypatch, down)
    assert await pairing.fetch_me("https://c", "tok") is None


# ------------------------------------------------------------------ one stack


def test_the_ui_channel_only_serves_with_aiohttp():
    # The engine's outbound HTTP goes through httpx (which loads certifi);
    # the UI channel's own aiohttp client for pairing, the profile and the
    # Feishu webhook was the one path that failed when the frozen build's
    # trust store was empty, and it retried the webhook with ssl=False.
    source = (ROOT / "mira_engine" / "channels" / "ui.py").read_text(encoding="utf-8")
    assert "ClientSession" not in source and "ClientTimeout" not in source and "ClientError" not in source
    assert "ssl=False" not in source
    assert "import httpx" in source
