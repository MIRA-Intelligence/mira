"""The user's community inbox: notifications filtered by the kinds they chose."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from typer.testing import CliRunner

from mira_engine.community import inbox
from mira_engine.config.schema import Config

ALL = list(inbox.NOTIFICATION_KINDS)


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("mira_engine.community.inbox.get_data_dir", lambda: tmp_path / "data")


def _reply(n=1, **extra):
    return {"type": "reply", "target_id": f"c{n}", "thread_id": "t1", "author": "bob-agent", "proposal_title": "Plugins",
            "title": 'bob-agent replied to your post "Plugins"', "content": "Nice idea", "on": "post", **extra}


def test_events_are_classified_into_the_kinds_users_choose_from():
    assert inbox.kind_of({"type": "reply"}) == "reply"
    assert inbox.kind_of({"type": "mention"}) == "mention"
    assert inbox.kind_of({"type": "needs_review"}) == "review"
    assert inbox.kind_of({"type": "pr_reverted"}) == inbox.kind_of({"type": "work_order_lease"}) == "work"
    assert inbox.kind_of({"type": "needs_vote"}) == inbox.kind_of({"type": "badge_earned"}) == "activity"
    for housekeeping in ("onboarding", "rules_updated", "ready", "error"):
        assert inbox.kind_of({"type": housekeeping}) is None


def test_default_choice_matches_the_config_default():
    assert Config().community.notifications == inbox.DEFAULT_NOTIFICATIONS


def test_only_chosen_kinds_are_filed_and_each_event_once():
    assert inbox.record_event(_reply(), ["mention"]) is None
    assert inbox.list_items() == []
    item = inbox.record_event(_reply(), ["reply"])
    assert item and item["from"] == "bob-agent" and item["thread_id"] == "t1" and item["body"] == "Nice idea"
    assert item["subject"] == "Plugins" and item["on"] == "post"
    assert inbox.record_event(_reply(), ["reply"]) is None  # same comment, delivered again
    assert len(inbox.list_items()) == 1


def test_a_reply_that_mentions_the_user_reaches_mention_followers():
    # Follows mentions only: filed where they look for it.
    assert inbox.record_event(_reply(1, mentioned=True), ["mention"])["kind"] == "mention"
    assert inbox.record_event(_reply(2), ["mention"]) is None
    # Follows both: one message, under replies.
    assert inbox.record_event(_reply(3, mentioned=True), ["reply", "mention"])["kind"] == "reply"
    assert inbox.unread_counts()["by_kind"] == {"reply": 1, "mention": 1, "review": 0, "work": 0, "activity": 0}


def test_items_without_a_title_get_a_readable_one():
    inbox.record_event({"type": "work_order_lease", "work_order_id": "w1", "state": "expired"}, ALL)
    inbox.record_event({"type": "work_order_lease", "work_order_id": "w1", "state": "reopened"}, ALL)
    inbox.record_event({"type": "pr_reviewed", "pr_id": "p1", "verdict": "approve"}, ALL)
    inbox.record_event({"type": "level_up", "level": 2, "label": "Lv2", "name": "Contributor"}, ALL)
    titles = [i["title"] for i in inbox.list_items()]
    assert titles == [
        "Your account reached Lv2 Contributor",
        "Your pull request was reviewed (approve)",
        "A work order you held is open again",
        "Your work order lease expired",
    ]


def test_unread_counts_and_marking_read():
    inbox.record_event(_reply(1), ALL)
    inbox.record_event(_reply(2), ALL)
    inbox.record_event({"type": "mention", "target_id": "c3", "title": "m"}, ALL)
    assert inbox.unread_counts() == {"total": 3, "by_kind": {"reply": 2, "mention": 1, "review": 0, "work": 0, "activity": 0}}
    newest = inbox.list_items()[0]
    assert inbox.mark_read([newest["id"]]) == 1
    assert inbox.mark_read(kind="reply") == 2
    assert inbox.unread_counts()["total"] == 0
    assert [i["read"] for i in inbox.list_items()] == [True, True, True]
    inbox.record_event(_reply(4), ALL)
    assert inbox.mark_read(everything=True) == 1


def test_inbox_keeps_only_the_newest_items(monkeypatch):
    monkeypatch.setattr(inbox, "MAX_ITEMS", 3)
    for n in range(5):
        inbox.record_event(_reply(n), ALL)
    assert [i["key"] for i in inbox.list_items()] == ["reply:c4", "reply:c3", "reply:c2"]


def test_normalize_kinds():
    assert inbox.normalize_kinds(["work", "reply"]) == ["reply", "work"]
    assert inbox.normalize_kinds([]) == []
    with pytest.raises(ValueError, match="spam"):
        inbox.normalize_kinds(["spam"])


# --------------------------------------------------------------------------- #
# The community channel files events; the agent still gets every one          #
# --------------------------------------------------------------------------- #


async def test_channel_files_chosen_events_and_still_wakes_the_agent(monkeypatch):
    from mira_engine.channels.community import CommunityChannel

    cfg = Config()
    cfg.community.notifications = ["reply"]
    monkeypatch.setattr("mira_engine.config.loader.load_config", lambda *_a, **_k: cfg)
    bus = SimpleNamespace(publish_inbound=AsyncMock())
    channel = CommunityChannel(SimpleNamespace(api_base="http://x", agent_token="t", agent_id="a", domains=[]), bus)

    await channel._handle_event(_reply())
    await channel._handle_event({"type": "needs_vote", "target_id": "p9", "title": "Vote?"})
    assert [i["type"] for i in inbox.list_items()] == ["reply"]
    assert bus.publish_inbound.await_count == 2  # the agent handles both

    cfg.community.notifications = []  # "no notifications": fully on its own
    await channel._handle_event(_reply(2))
    assert len(inbox.list_items()) == 1 and bus.publish_inbound.await_count == 3


# --------------------------------------------------------------------------- #
# Desktop endpoints                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def ui(tmp_path, monkeypatch):
    from mira_engine.channels import ui as ui_mod

    cfg = Config()
    saved: list = []
    monkeypatch.setattr(ui_mod.config_loader, "get_config_path", lambda: tmp_path / "c.json")
    monkeypatch.setattr(ui_mod.config_loader, "load_config", lambda _p=None: cfg)
    monkeypatch.setattr(ui_mod.config_loader, "save_config", lambda c, _p=None: saved.append(list(c.community.notifications)))
    return SimpleNamespace(mod=ui_mod, cfg=cfg, saved=saved, channel=SimpleNamespace(_audit=lambda **_k: None))


def _req(query=None, body=None):
    req = MagicMock(spec=web.Request)
    req.query = query or {}
    req.json = AsyncMock(return_value=body)
    return req


async def test_inbox_endpoint_lists_and_filters(ui):
    inbox.record_event(_reply(1), ALL)
    inbox.record_event({"type": "mention", "target_id": "c3", "title": "m"}, ALL)
    resp = await ui.mod.UiChannel._handle_community_inbox(ui.channel, _req({"kind": "mention"}))
    body = json.loads(resp.text)
    assert [i["type"] for i in body["items"]] == ["mention"] and body["unread"]["total"] == 2
    resp = await ui.mod.UiChannel._handle_community_inbox(ui.channel, _req({"kind": "spam"}))
    assert resp.status == 400


async def test_mark_read_endpoint(ui):
    item = inbox.record_event(_reply(1), ALL)
    inbox.record_event(_reply(2), ALL)
    resp = await ui.mod.UiChannel._handle_community_inbox_read(ui.channel, _req(body={"ids": [item["id"]]}))
    assert json.loads(resp.text) == {"changed": 1, "unread": inbox.unread_counts()} and inbox.unread_counts()["total"] == 1
    resp = await ui.mod.UiChannel._handle_community_inbox_read(ui.channel, _req(body={"all": True}))
    assert json.loads(resp.text)["changed"] == 1
    for bad in ({}, {"ids": "x"}, {"kind": "spam"}, ["all"]):
        assert (await ui.mod.UiChannel._handle_community_inbox_read(ui.channel, _req(body=bad))).status == 400


async def test_notifications_endpoint_saves_the_choice(ui):
    resp = await ui.mod.UiChannel._handle_community_notifications(ui.channel, _req(body={"kinds": ["mention", "reply"]}))
    assert json.loads(resp.text) == {"notifications": ["reply", "mention"]} and ui.saved[-1] == ["reply", "mention"]
    resp = await ui.mod.UiChannel._handle_community_notifications(ui.channel, _req(body={"kinds": []}))
    assert json.loads(resp.text) == {"notifications": []}
    assert (await ui.mod.UiChannel._handle_community_notifications(ui.channel, _req(body={"kinds": ["x"]}))).status == 400


async def test_status_reports_notifications_unread_and_the_website(ui, monkeypatch):
    inbox.record_event(_reply(1), ALL)
    ui.cfg.community.agent_token = "t"
    ui.cfg.community.api_base = "http://local:8099"

    async def me(_cfg):
        return {"status": "active", "web_url": "https://web.example/"}

    monkeypatch.setattr(ui.mod.UiChannel, "_fetch_agent_me", staticmethod(me))
    monkeypatch.setattr(ui.mod.community_approvals, "list_approvals", lambda status=None: [])
    body = json.loads((await ui.mod.UiChannel._handle_community_status(ui.mod.UiChannel, MagicMock(spec=web.Request))).text)
    assert body["member_status"] == "active" and body["web_url"] == "https://web.example"
    assert body["notifications"] == inbox.DEFAULT_NOTIFICATIONS and body["unread"]["total"] == 1


def test_website_resolution_order():
    from mira_engine.channels.ui import UiChannel

    cfg = Config()
    assert UiChannel._community_web_url(cfg, None) == "https://community.mira-intelligence.com"  # production API
    cfg.community.api_base = "http://192.168.3.166:8099"
    assert UiChannel._community_web_url(cfg, None) == ""  # local server advertising nothing
    assert UiChannel._community_web_url(cfg, {"web_url": "http://web.local/"}) == "http://web.local"
    cfg.community.web_url = "https://mine.example"
    assert UiChannel._community_web_url(cfg, {"web_url": "http://web.local"}) == "https://mine.example"


def test_cli_shows_and_sets_notifications(tmp_path):
    from mira_engine.cli.commands import app

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}")
    run = lambda *a: CliRunner().invoke(app, ["community", "notifications", *a, "--config", str(cfg_file)])
    assert "reply, mention, review, work" in run().output
    assert run("mention").exit_code == 0 and json.loads(cfg_file.read_text())["community"]["notifications"] == ["mention"]
    assert "none" in run("none").output and json.loads(cfg_file.read_text())["community"]["notifications"] == []
    bad = run("spam")
    assert bad.exit_code == 1 and "unknown notification kinds" in bad.output
