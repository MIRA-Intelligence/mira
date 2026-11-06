"""Usage windows of ChatGPT plans and the team caps on them."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import mira_engine.providers.openai_codex_provider as codex_provider
from mira_engine.config import team_layer
from mira_engine.providers import subscription_quota as sq
from mira_engine.providers.openai_codex_provider import OpenAICodexProvider, _request_codex_once

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean():
    sq.clear()
    team_layer.clear_team_layer()
    yield
    sq.clear()
    team_layer.clear_team_layer()


def _caps(**caps: int | None) -> None:
    team_layer.set_team_layer(team_layer.parse_layer({"subscription_quota": {sq.CODEX: caps}}))


def _headers(**windows: tuple[float, int | None, float | None]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for slot, (used, minutes, resets_at) in windows.items():
        headers[f"x-codex-{slot}-used-percent"] = str(used)
        if minutes is not None:
            headers[f"x-codex-{slot}-window-minutes"] = str(minutes)
        if resets_at is not None:
            headers[f"x-codex-{slot}-reset-at"] = str(int(resets_at))
    return headers


def _record(account_id: str = "acct", **windows: tuple[float, int | None, float | None]) -> None:
    sq.record(sq.CODEX, sq.parse_codex_headers(_headers(**windows), account_id=account_id, now=NOW))


# ── reading the headers ──────────────────────────────────────────────


def test_windows_are_told_apart_by_length() -> None:
    usage = sq.parse_codex_headers(_headers(primary=(12.5, 300, NOW + 600), secondary=(40, 10080, NOW + 86400)),
                                   now=NOW)
    assert usage is not None
    assert usage.windows == {sq.FIVE_HOUR: sq.RateWindow(12.5, 300, NOW + 600),
                             sq.WEEKLY: sq.RateWindow(40.0, 10080, NOW + 86400)}
    # A plan without a 5-hour window: its only window is the weekly one, whatever the slot.
    usage = sq.parse_codex_headers(_headers(primary=(7, 10080, None)), now=NOW)
    assert usage is not None and set(usage.windows) == {sq.WEEKLY}


def test_reset_after_seconds_and_missing_headers() -> None:
    usage = sq.parse_codex_headers({"x-codex-primary-used-percent": "3", "x-codex-primary-reset-after-seconds": "60",
                                    "x-codex-rate-limit-reached-type": "primary"}, now=NOW)
    assert usage is not None and usage.windows[sq.FIVE_HOUR].resets_at == NOW + 60
    assert usage.limit_reached == "primary"
    assert sq.parse_codex_headers({"content-type": "text/event-stream"}) is None
    assert sq.parse_codex_headers({"x-codex-primary-used-percent": "n/a"}) is None


# ── the team caps ────────────────────────────────────────────────────


def test_a_window_at_its_cap_refuses_until_it_resets() -> None:
    _caps(five_hour=80, weekly=90)
    _record(primary=(50, 300, NOW + 600), secondary=(90, 10080, NOW + 86400))
    message = sq.refusal(sq.CODEX, "acct", now=NOW)
    assert message is not None and "weekly window" in message and "90% used, Mira may use up to 90%" in message
    assert sq.refusal(sq.CODEX, "acct", now=NOW + 86400) is None  # reset
    assert sq.refusal(sq.CODEX, "other-account", now=NOW) is None  # signed in to another account
    _caps(five_hour=80, weekly=95)
    assert sq.refusal(sq.CODEX, "acct", now=NOW) is None


def test_a_plan_without_a_five_hour_window_is_held_to_its_weekly_cap_only() -> None:
    _caps(five_hour=10, weekly=50)
    _record(primary=(30, 10080, NOW + 86400))
    assert sq.refusal(sq.CODEX, "acct", now=NOW) is None
    _record(primary=(55, 10080, NOW + 86400))
    assert "weekly" in (sq.refusal(sq.CODEX, "acct", now=NOW) or "")


def test_no_caps_or_no_usage_yet_never_refuse() -> None:
    _record(primary=(100, 300, NOW + 600))
    assert sq.refusal(sq.CODEX, "acct", now=NOW) is None  # no team caps
    sq.clear()
    _caps(weekly=1)
    assert sq.refusal(sq.CODEX, "acct", now=NOW) is None  # nothing reported yet


def test_usage_without_a_reset_time_counts_for_a_while() -> None:
    _caps(five_hour=50)
    _record(primary=(60, 300, None))
    assert sq.refusal(sq.CODEX, "acct", now=NOW + 10) is not None
    assert sq.refusal(sq.CODEX, "acct", now=NOW + sq.STALE_AFTER_S) is None  # one request may learn more


def test_status_reports_windows_and_caps() -> None:
    _caps(weekly=90)
    _record(primary=(20, 300, NOW + 600), secondary=(95, 10080, NOW + 86400))
    status = sq.status(now=NOW)["providers"][sq.CODEX]
    assert status["limits"] == {"five_hour": None, "weekly": 90}
    assert status["windows"]["weekly"] | {"resets_at": None} == {
        "used_percent": 95.0, "window_minutes": 10080, "resets_at": None, "current": True,
        "limit_percent": 90, "exceeded": True}
    assert status["windows"]["five_hour"]["exceeded"] is False
    assert sq.status(now=NOW + 86400)["providers"][sq.CODEX]["windows"]["weekly"]["exceeded"] is False


@pytest.mark.parametrize("caps, message", [
    ({"weekly": 101}, "percentage"),
    ({"weekly": "90"}, "percentage"),
    ([], "must be an object"),
])
def test_layer_rejects_invalid_caps(caps, message: str) -> None:
    data = {"subscription_quota": {sq.CODEX: caps}}
    with pytest.raises(team_layer.TeamLayerError, match=message):
        team_layer.parse_layer(data)


def test_layer_drops_no_caps_and_unknown_windows() -> None:
    layer = team_layer.parse_layer({"subscription_quota": {sq.CODEX: {"five_hour": 0, "weekly": None,
                                                                      "monthly": 50}}})
    assert layer.subscription_quota == {} and layer.empty
    layer = team_layer.parse_layer({"subscription_quota": {sq.CODEX: {"weekly": 90}}})
    assert layer.subscription_quota == {sq.CODEX: {"weekly": 90}} and not layer.empty


# ── the Codex provider ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_codex_refuses_without_calling_chatgpt_when_a_cap_is_reached(monkeypatch) -> None:
    _caps(weekly=50)
    _record(primary=(60, 10080, NOW + 86400))
    monkeypatch.setattr(sq.time, "time", lambda: NOW)
    monkeypatch.setattr(codex_provider, "ensure_oauth_state_dirs_for_runtime", lambda: None)
    monkeypatch.setattr(codex_provider, "get_codex_token", lambda: SimpleNamespace(access="t", account_id="acct"))

    async def never(*args, **kwargs):
        raise AssertionError("the request went out")

    monkeypatch.setattr(codex_provider, "_request_codex", never)
    response = await OpenAICodexProvider().chat([{"role": "user", "content": "hi"}])
    assert response.finish_reason == "error" and response.error_status_code == 429
    assert response.error_should_retry is False
    assert "weekly window" in (response.content or "")
    assert not OpenAICodexProvider._is_transient_response(response)


def _mock_client(monkeypatch, handler) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(codex_provider.httpx, "AsyncClient",
                        lambda **kwargs: real(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_codex_responses_record_the_plan_usage(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = b'data: {"type": "response.completed", "response": {"status": "completed"}}\n\n'
        return httpx.Response(200, content=body, headers={
            "content-type": "text/event-stream", **_headers(primary=(12, 300, NOW + 600))})

    _mock_client(monkeypatch, handler)
    await _request_codex_once("https://chatgpt.test/codex/responses", {"chatgpt-account-id": "acct"}, {},
                              verify=True)
    usage = sq.latest(sq.CODEX)
    assert usage is not None and usage.account_id == "acct"
    assert usage.windows[sq.FIVE_HOUR].used_percent == 12


@pytest.mark.asyncio
async def test_a_429_from_codex_says_which_plan_window_ran_out(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "usage limit"}},
                              headers=_headers(primary=(30, 300, NOW + 600), secondary=(100, 10080, NOW + 3600)))

    _mock_client(monkeypatch, handler)
    with pytest.raises(codex_provider.CodexAPIError) as raised:
        await _request_codex_once("https://chatgpt.test/codex/responses", {}, {}, verify=True)
    assert raised.value.status_code == 429
    assert "quota exceeded" in str(raised.value) and "weekly limit is used up" in str(raised.value)
    assert sq.latest(sq.CODEX).windows[sq.WEEKLY].used_percent == 100
