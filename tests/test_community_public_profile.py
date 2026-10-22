"""The public profile collected when joining (design §11)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from mira_engine.community.public_profile import (
    PublicProfileError,
    normalize_public_profile,
    pairing_payload,
    split_list,
)


def test_normalize_trims_dedupes_and_drops_empty_fields():
    out = normalize_public_profile(
        {"domains": "plugins, agents，plugins", "tech_stack": ["Python", " "], "languages": [], "participation": "Review,develop", "bio": "  hi  "}
    )
    assert out == {"domains": ["plugins", "agents"], "tech_stack": ["Python"], "participation": ["review", "develop"], "bio": "hi"}
    assert normalize_public_profile(None) == {} and normalize_public_profile({"bio": ""}) == {}
    assert split_list(" a ,, b ") == ["a", "b"]


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"participation": "lurk"}, "participation"),
        ({"bio": "x" * 501}, "500"),
        ({"tech_stack": ["x" * 41]}, "40"),
        ({"domains": [str(i) for i in range(21)]}, "20"),
        ({"languages": [1, 2]}, "languages"),
    ],
)
def test_normalize_rejects_what_the_platform_would(raw, message):
    with pytest.raises(PublicProfileError, match=message):
        normalize_public_profile(raw)


def test_pairing_payload_keeps_domains_top_level_for_older_platforms():
    payload = pairing_payload({"code_host": "github", "domains": []}, {"domains": ["rag"], "bio": "hi"})
    assert payload["domains"] == ["rag"] and payload["profile"] == {"domains": ["rag"], "bio": "hi"}
    assert "profile" not in pairing_payload({"domains": ["x"]}, {})


# --------------------------------------------------------------------------- #
# `mira community login`                                                       #
# --------------------------------------------------------------------------- #


def _fake_platform(monkeypatch, *, start_status=200, start_body=None):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/pair/start"):
            seen["start"] = json.loads(request.content)
            if start_status != 200:
                return httpx.Response(start_status, json=start_body or {})
            return httpx.Response(200, json={"user_code": "ABCD-EFGH", "poll_token": "p", "verification_url": "http://x/pair", "interval": 0})
        if request.url.path.endswith("/agents/pair/poll"):
            return httpx.Response(200, json={"status": "authorized", "agent_token": "tok", "agent_id": "a1"})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(*a, transport=httpx.MockTransport(handler), **k))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr("webbrowser.open", lambda _u: None)
    return seen


def _login(tmp_path, *args):
    from mira_engine.cli.commands import app

    cfg = tmp_path / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")
    result = CliRunner().invoke(app, ["community", "login", "--api-base", "http://x", "--config", str(cfg), *args])
    return result, json.loads(cfg.read_text(encoding="utf-8"))


def test_login_sends_the_public_profile_and_points_to_the_private_one(tmp_path, monkeypatch):
    seen = _fake_platform(monkeypatch)
    result, saved = _login(
        tmp_path, "--domains", "rag,agents", "--tech-stack", "python", "--participation", "review", "--bio", "RAG person"
    )
    assert result.exit_code == 0, result.output
    assert seen["start"]["domains"] == ["rag", "agents"]
    assert seen["start"]["profile"] == {"domains": ["rag", "agents"], "tech_stack": ["python"], "participation": ["review"], "bio": "RAG person"}
    assert saved["community"]["agentToken"] == "tok" and saved["community"]["domains"] == ["rag", "agents"]
    assert "mira profile" in result.output  # not onboarded, no TTY: a tip instead of a prompt
    assert "Restart the gateway" not in result.output


def test_login_rejects_a_bad_profile_before_contacting_the_platform(tmp_path, monkeypatch):
    seen = _fake_platform(monkeypatch)
    result, _ = _login(tmp_path, "--participation", "lurk")
    assert result.exit_code == 1 and "participation" in result.output and "start" not in seen


def test_login_reports_the_platforms_reason_for_a_rejected_profile(tmp_path, monkeypatch):
    _fake_platform(monkeypatch, start_status=400, start_body={"error": "bio must be a string of at most 500 characters"})
    result, _ = _login(tmp_path, "--bio", "fine here")
    assert result.exit_code == 1 and "bio must be a string" in result.output and "Network error" not in result.output


# --------------------------------------------------------------------------- #
# Desktop UI pairing endpoint                                                  #
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _FakeSession:
    def __init__(self, sent, status=200, body=None):
        self.sent, self.status, self.body = sent, status, body or {"user_code": "C", "poll_token": "p", "verification_url": "u"}

    def post(self, url, json=None):
        self.sent.append((url, json))
        return _FakeResp(self.status, self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


@pytest.fixture
def ui_env(tmp_path, monkeypatch):
    from mira_engine.channels import ui as ui_mod
    from mira_engine.config.schema import Config

    cfg = Config()
    cfg.community.api_base = "http://community"
    monkeypatch.setattr(ui_mod.config_loader, "get_config_path", lambda: tmp_path / "c.json")
    monkeypatch.setattr(ui_mod.config_loader, "load_config", lambda _p=None: cfg)
    monkeypatch.setattr(ui_mod.config_loader, "save_config", lambda *a, **k: None)
    sent: list = []
    return SimpleNamespace(mod=ui_mod, cfg=cfg, sent=sent, monkeypatch=monkeypatch)


def _request(body):
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    req = MagicMock(spec=web.Request)
    req.json = AsyncMock(return_value=body)
    return req


async def test_ui_pairing_forwards_the_public_profile(ui_env):
    ui_env.monkeypatch.setattr(ui_env.mod, "ClientSession", lambda **_k: _FakeSession(ui_env.sent))
    channel = SimpleNamespace()
    resp = await ui_env.mod.UiChannel._handle_community_pair_start(
        channel, _request({"profile": {"domains": ["rag"], "tech_stack": "python, go", "bio": "hi"}})
    )
    assert resp.status == 200
    url, payload = ui_env.sent[0]
    assert url == "http://community/agents/pair/start"
    assert payload["domains"] == ["rag"] and payload["profile"]["tech_stack"] == ["python", "go"]
    assert ui_env.cfg.community.domains == ["rag"]


async def test_ui_pairing_rejects_a_bad_profile_and_relays_platform_errors(ui_env):
    ui_env.monkeypatch.setattr(ui_env.mod, "ClientSession", lambda **_k: _FakeSession(ui_env.sent))
    resp = await ui_env.mod.UiChannel._handle_community_pair_start(SimpleNamespace(), _request({"profile": {"bio": "x" * 600}}))
    assert resp.status == 400 and "500" in json.loads(resp.text)["error"] and ui_env.sent == []

    ui_env.monkeypatch.setattr(
        ui_env.mod, "ClientSession", lambda **_k: _FakeSession(ui_env.sent, 400, {"error": "participation must be a subset"})
    )
    resp = await ui_env.mod.UiChannel._handle_community_pair_start(SimpleNamespace(), _request({"profile": {"bio": "ok"}}))
    assert resp.status == 400 and "participation must be a subset" in json.loads(resp.text)["error"]


def test_status_reports_whether_the_private_profile_is_set_up():
    from mira_engine.channels.ui import UiChannel
    from mira_engine.config.schema import Config

    cfg = Config()
    assert UiChannel._community_status_payload(cfg)["profile_onboarded"] is False
    cfg.profile.onboarded = True
    assert UiChannel._community_status_payload(cfg)["profile_onboarded"] is True
