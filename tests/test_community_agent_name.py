"""The agent's community name, its editable public profile, and @mentions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiohttp import web
from typer.testing import CliRunner

from mira_engine.community import mentions
from mira_engine.community.public_profile import (
    PublicProfileError,
    normalize_agent_name,
    normalize_profile_patch,
    normalize_public_profile,
    suggested_agent_name,
)
from mira_engine.config.schema import Config
from mira_engine.profile.onboarding import render_soul_local

_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any test patches it

# ------------------------------------------------------------------ validation


@pytest.mark.parametrize("name", ["MIRAI", "nova-2", "a", "小米", "Ada_Lovelace.v2", "x" * 32])
def test_valid_names(name):
    assert normalize_agent_name(f"  {name} ") == name


@pytest.mark.parametrize("name", ["", "two words", "@nova", "-nova", "_nova", "x" * 33, "a@b", None, 7])
def test_invalid_names(name):
    with pytest.raises(PublicProfileError):
        normalize_agent_name(name)


def test_join_profile_carries_the_name():
    assert normalize_public_profile({"name": "Nova", "bio": "hi"}) == {"name": "Nova", "bio": "hi"}
    with pytest.raises(PublicProfileError):
        normalize_public_profile({"name": "two words"})


def test_an_edit_changes_only_the_fields_sent_and_can_clear_them():
    assert normalize_profile_patch({"name": "Nova"}) == {"name": "Nova"}
    assert normalize_profile_patch({"bio": "", "tech_stack": [], "languages": "en, zh"}) == {
        "bio": None,
        "tech_stack": [],
        "languages": ["en", "zh"],
    }
    for bad in ({"name": ""}, {"participation": ["lurk"]}, {"bio": "x" * 501}, {"secret": 1}, ["name"]):
        with pytest.raises(PublicProfileError):
            normalize_profile_patch(bad)


def test_the_suggested_name_is_the_soul_name_when_it_is_a_valid_handle(tmp_path):
    (tmp_path / "SOUL.local.md").write_text(render_soul_local({"name": "MIRAI"}), encoding="utf-8")
    assert suggested_agent_name(tmp_path) == "MIRAI"
    (tmp_path / "SOUL.local.md").write_text(render_soul_local({"name": "Mira Two"}), encoding="utf-8")
    assert suggested_agent_name(tmp_path) is None  # has a space; the user picks one when joining
    assert suggested_agent_name(tmp_path / "missing") is None


# ------------------------------------------------------------------ prompts


def test_the_mention_note_names_the_agent_and_says_how_to_address_others():
    note = mentions.mention_note("Nova", "35f0bd8e-e032-46b1")
    assert "your name is Nova (#35f0bd8e)" in note and "@Nova" in note
    assert "start with @TheirName" in note and "Never @mention yourself (@Nova)" in note
    assert "start with @TheirName" in mentions.mention_note(None, None)


def test_community_turns_get_the_mention_note_and_follow_a_rename_live(tmp_path, monkeypatch):
    from mira_engine.agent.base_loop import BaseAgentLoop
    from mira_engine.config.schema import ExecToolConfig, PythonRuntimeConfig

    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("mira_engine.config.loader.get_config_path", lambda: cfg_file)
    loop = SimpleNamespace(
        exec_config=ExecToolConfig(python=PythonRuntimeConfig(manager="off")),
        community_config=SimpleNamespace(rules_text="", agent_name="Nova", agent_id="35f0bd8e-xxxx"),
    )
    out = BaseAgentLoop._compose_extra_system(loop, None, None, channel="community")
    assert "your name is Nova (#35f0bd8e)" in out
    cfg_file.write_text(json.dumps({"community": {"agentName": "Vega"}}))  # renamed in the app
    assert "your name is Vega" in BaseAgentLoop._compose_extra_system(loop, None, None, channel="community")
    # Other channels get the discovery hint (#72), not the mention note.
    ui_out = BaseAgentLoop._compose_extra_system(loop, None, None, channel="ui")
    assert "your name is" not in ui_out and "Mira Community" in ui_out


def test_reply_events_show_whom_to_mention():
    from mira_engine.channels.community import CommunityChannel

    text = CommunityChannel._format_event(
        {"type": "reply", "title": 'Ada replied to your post "X"', "author": "Ada", "content": "Try this."}
    )
    assert "from: @Ada" in text and "start your reply with @Ada" in text
    plain = CommunityChannel._format_event({"type": "needs_review", "title": "Review PR", "author": "Ada"})
    assert "@Ada" not in plain


def test_the_comment_tool_says_to_mention_the_other_agent():
    from mira_engine.agent.tools.community import CommunityCommentTool

    assert "@TheirName" in CommunityCommentTool(SimpleNamespace(), SimpleNamespace()).description


async def test_onboarding_introduces_the_agent_by_its_community_name(tmp_path, monkeypatch):
    from mira_engine.community.onboarding import build_onboarding_prompt

    monkeypatch.setattr("mira_engine.config.paths.get_workspace_path", lambda *_a: tmp_path)
    (tmp_path / "SOUL.local.md").write_text(render_soul_local({"name": "Soulname"}), encoding="utf-8")
    cfg = SimpleNamespace(rules_version=6, agent_name="Nova")
    assert "Your name is Nova" in await build_onboarding_prompt(cfg, tmp_path)
    cfg.agent_name = ""
    assert "Your name is Soulname" in await build_onboarding_prompt(cfg, tmp_path)


# ------------------------------------------------------------------ UI endpoints


@pytest.fixture
def ui(tmp_path, monkeypatch):
    from mira_engine.channels import ui as ui_mod

    cfg = Config()
    cfg.community.agent_token = "tok"
    cfg.community.agent_id = "35f0bd8e-e032-46b1-bb0c-c134bdc21fb1"
    cfg.community.api_base = "http://local:8099"
    saved: list = []
    monkeypatch.setattr(ui_mod.config_loader, "get_config_path", lambda: tmp_path / "c.json")
    monkeypatch.setattr(ui_mod.config_loader, "load_config", lambda _p=None: cfg)
    monkeypatch.setattr(
        ui_mod.config_loader,
        "save_config",
        lambda c, _p=None: saved.append((c.community.agent_name, list(c.community.domains))),
    )
    return SimpleNamespace(mod=ui_mod, cfg=cfg, saved=saved, channel=SimpleNamespace(_audit=lambda **_k: None))


def _req(body=None):
    req = MagicMock(spec=web.Request)
    req.query = {}
    req.json = AsyncMock(return_value=body)
    return req


def _fake_session(calls, status, data):
    """The platform behind every httpx.AsyncClient: records what was sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), json.loads(request.content), request.headers))
        return httpx.Response(status, json=data)

    return lambda *a, **k: _REAL_ASYNC_CLIENT(*a, transport=httpx.MockTransport(handler), **k)


async def test_get_profile_returns_the_platform_view(ui, monkeypatch):
    async def me(_cfg):
        return {"id": ui.cfg.community.agent_id, "name": "Nova", "profile": {"bio": "hi", "domains": ["agents"]}}

    monkeypatch.setattr(ui.mod.UiChannel, "_fetch_agent_me", staticmethod(me))
    resp = await ui.mod.UiChannel._handle_community_profile(ui.mod.UiChannel, _req())
    assert json.loads(resp.text) == {
        "agent_id": ui.cfg.community.agent_id,
        "name": "Nova",
        "profile": {"bio": "hi", "domains": ["agents"]},
    }


async def test_get_profile_when_logged_out_or_offline(ui, monkeypatch):
    async def offline(_cfg):
        return None

    monkeypatch.setattr(ui.mod.UiChannel, "_fetch_agent_me", staticmethod(offline))
    assert (await ui.mod.UiChannel._handle_community_profile(ui.mod.UiChannel, _req())).status == 502
    ui.cfg.community.agent_token = ""
    assert (await ui.mod.UiChannel._handle_community_profile(ui.mod.UiChannel, _req())).status == 409


async def test_saving_the_profile_patches_the_platform_and_caches_the_name(ui, monkeypatch):
    calls: list = []
    platform = {"name": "Vega", "profile": {"domains": ["plugins"], "bio": "new"}}
    monkeypatch.setattr(httpx, "AsyncClient",_fake_session(calls, 200, platform))
    resp = await ui.mod.UiChannel._handle_community_profile_update(
        ui.channel, _req({"name": " Vega ", "bio": "new", "domains": ["plugins"]})
    )
    assert resp.status == 200
    assert json.loads(resp.text) == {"agent_id": ui.cfg.community.agent_id, **platform}
    url, sent, headers = calls[0]
    assert url == "http://local:8099/agents/me/profile" and headers["Authorization"] == "Bearer tok"
    assert sent == {"name": "Vega", "bio": "new", "domains": ["plugins"]}
    assert ui.saved[-1] == ("Vega", ["plugins"])


async def test_the_app_sends_every_field_and_cleared_ones_reach_the_platform_as_clears(ui, monkeypatch):
    # mira-ui sends the whole form; an emptied bio or list must clear it, not be dropped.
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient",_fake_session(calls, 200, {"name": "Nova", "profile": {}}))
    form = {"name": "Nova", "domains": ["agents"], "tech_stack": [], "languages": ["en"], "participation": ["answer"], "bio": ""}
    resp = await ui.mod.UiChannel._handle_community_profile_update(ui.channel, _req(form))
    assert resp.status == 200
    assert calls[0][1] == {**form, "bio": None}  # platform: bio null clears it, [] clears a list


async def test_saving_rejects_bad_input_before_reaching_the_platform(ui, monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient",_fake_session(calls, 200, {}))
    for bad in ({"name": "two words"}, {}, {"token": "x"}, "Nova"):
        resp = await ui.mod.UiChannel._handle_community_profile_update(ui.channel, _req(bad))
        assert resp.status == 400 and json.loads(resp.text)["error"]
    assert calls == [] and ui.saved == []


async def test_saving_surfaces_the_platform_refusal_and_keeps_the_cache(ui, monkeypatch):
    ui.cfg.community.agent_name = "Nova"
    monkeypatch.setattr(httpx, "AsyncClient",_fake_session([], 400, {"error": "name taken"}))
    resp = await ui.mod.UiChannel._handle_community_profile_update(ui.channel, _req({"name": "Vega"}))
    assert resp.status == 400 and "name taken" in json.loads(resp.text)["error"]
    assert ui.cfg.community.agent_name == "Nova" and ui.saved == []


async def test_an_older_platform_that_ignores_the_name_keeps_the_cached_one(ui, monkeypatch):
    ui.cfg.community.agent_name = "Nova"
    monkeypatch.setattr(httpx, "AsyncClient",_fake_session([], 200, {"profile": {"bio": "x"}}))
    resp = await ui.mod.UiChannel._handle_community_profile_update(ui.channel, _req({"name": "Vega", "bio": "x"}))
    assert json.loads(resp.text)["name"] == "Nova"


async def test_status_reports_the_name_and_the_suggested_one(ui, monkeypatch, tmp_path):
    ui.cfg.agents.defaults.workspace = str(tmp_path)
    (tmp_path / "SOUL.local.md").write_text(render_soul_local({"name": "MIRAI"}), encoding="utf-8")

    async def me(_cfg):
        return {"status": "active", "name": "Nova"}

    monkeypatch.setattr(ui.mod.UiChannel, "_fetch_agent_me", staticmethod(me))
    monkeypatch.setattr(ui.mod.community_approvals, "list_approvals", lambda status=None: [])
    body = json.loads((await ui.mod.UiChannel._handle_community_status(ui.mod.UiChannel, _req())).text)
    assert body["agent_name"] == "Nova" and body["suggested_name"] == "MIRAI"


# ------------------------------------------------------------------ CLI


def test_cli_profile_shows_and_updates(tmp_path, monkeypatch):
    from mira_engine.cli.commands import app

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"community": {"agentToken": "tok", "agentId": "35f0bd8e-1", "apiBase": "http://local"}}))
    state = {"name": "Nova", "profile": {"domains": ["agents"], "bio": None}}
    sent: list = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def patch(self, url, json=None, headers=None):
            sent.append(json)
            state["name"] = json.get("name", state["name"])
            return httpx.Response(200, json=state, request=httpx.Request("PATCH", url))

        def get(self, url, headers=None):
            return httpx.Response(200, json={"id": "35f0bd8e-1", **state}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "Client", Client)
    run = lambda *a: CliRunner().invoke(app, ["community", "profile", *a, "--config", str(cfg_file)])
    shown = run()
    assert shown.exit_code == 0 and "Nova" in shown.output and "#35f0bd8e" in shown.output and sent == []
    changed = run("--name", "Vega")
    assert changed.exit_code == 0 and sent == [{"name": "Vega"}]
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["community"]["agentName"] == "Vega"
    bad = run("--name", "two words")
    assert bad.exit_code == 1 and len(sent) == 1
