"""Tests for the heartbeat community digest (#112, #16)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mira_engine.community import client as client_mod
from mira_engine.community import digest


@pytest.fixture(autouse=True)
def _seen_record_in_tmp(monkeypatch, tmp_path):
    """The heartbeat's seen-record never touches the user's data dir in tests."""
    monkeypatch.setattr(digest, "get_data_dir", lambda: tmp_path / "data")


def _cfg(**kw):
    base = dict(
        enabled=True,
        api_base="http://x/community",
        agent_token="tok",
        domains=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _install_client(
    monkeypatch, *, tasks=None, rules=None, feed=None, tasks_raises=None, recorder=None
):
    """Install a fake CommunityClient with configurable task/rules/feed behavior.

    Rules sync is exercised for real against the fake client, but its on-disk
    persistence is neutralized so tests never touch the user's config.
    """

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get_tasks(self):
            if tasks_raises is not None:
                raise tasks_raises
            return {"tasks": tasks or []}

        async def get_rules(self):
            return rules or {"version": 1, "rules": [], "onboarding_thread_id": None}

        async def ack_rules(self, version):
            if recorder is not None:
                recorder.append(("ack", version))
            return {"ok": True, "acked_version": version}

        async def read_feed(self, limit=20, status=None):
            return {"proposals": feed or []}

    monkeypatch.setattr(client_mod, "CommunityClient", FakeClient)
    monkeypatch.setattr("mira_engine.community.rules._persist", lambda *a, **k: None)


async def test_disabled_returns_empty():
    assert await digest.gather_community_digest(_cfg(enabled=False)) == ""
    assert await digest.gather_community_digest(_cfg(agent_token="")) == ""


async def test_no_tasks_returns_empty(monkeypatch):
    _install_client(monkeypatch, tasks=[])
    assert await digest.gather_community_digest(_cfg()) == ""


async def test_tasks_render_next_actions(monkeypatch):
    _install_client(
        monkeypatch,
        tasks=[
            {
                "type": "needs_vote",
                "target_id": "p1",
                "title": 'Vote on "Add retry"',
                "reason": "open proposal in your area",
            }
        ],
        rules={"version": 2, "rules": [{"title": "No secrets"}], "onboarding_thread_id": None},
    )
    out = await digest.gather_community_digest(_cfg())
    assert "actionable item" in out
    assert "needs_vote" in out
    assert "p1" in out
    assert "community_vote" in out
    # Rules live in the agent's system prompt now — not repeated in the digest.
    assert "rules v2" not in out.lower()


async def test_rules_synced_and_ack_task_dropped(monkeypatch):
    recorder = []
    _install_client(
        monkeypatch,
        tasks=[
            {"type": "rules_ack", "target_id": "rules:1", "title": "Rules updated", "reason": "info"},
            {"type": "onboarding", "target_id": "ob", "title": "Connect", "reason": "verify"},
        ],
        rules={"version": 1, "rules": [{"title": "Be nice"}], "onboarding_thread_id": "ob"},
        recorder=recorder,
    )
    out = await digest.gather_community_digest(_cfg())
    # Rules are synced (silently acked) out of band; the informational rules_ack
    # task is dropped from the digest while onboarding remains.
    assert ("ack", 1) in recorder
    assert "rules_ack" not in out
    assert "onboarding" in out


async def test_rules_cached_on_config(monkeypatch):
    cfg = _cfg()
    _install_client(
        monkeypatch,
        tasks=[],
        rules={"version": 3, "rules": [{"title": "Be excellent", "text": "to each other"}]},
    )
    await digest.gather_community_digest(cfg)
    # The digest's sync caches the rules on the live config for prompt injection.
    assert cfg.rules_version == 3
    assert "Be excellent" in cfg.rules_text


async def test_falls_back_to_feed_when_tasks_unavailable(monkeypatch):
    _install_client(
        monkeypatch,
        tasks_raises=client_mod.CommunityError("HTTP 404"),
        feed=[{"id": "p1", "status": "open", "title": "Anything", "author_handle": "a"}],
    )
    out = await digest.gather_community_digest(_cfg())
    assert "Anything" in out
    assert "p1" in out


async def test_feed_fallback_respects_domains(monkeypatch):
    _install_client(
        monkeypatch,
        tasks_raises=client_mod.CommunityError("HTTP 404"),
        feed=[
            {
                "id": "p1",
                "status": "open",
                "title": "Improve chemistry tools",
                "author_handle": "a",
            },
            {"id": "p2", "status": "open", "title": "New billing page", "author_handle": "b"},
        ],
    )
    out = await digest.gather_community_digest(_cfg(domains=["chemistry"]))
    assert "p1" in out
    assert "p2" not in out


async def test_network_error_returns_empty(monkeypatch):
    class BoomClient:
        def __init__(self, *a, **k):
            pass

        async def get_tasks(self):
            raise RuntimeError("network down")

        async def read_feed(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(client_mod, "CommunityClient", BoomClient)
    assert await digest.gather_community_digest(_cfg()) == ""


async def test_a_task_is_presented_once_a_day_unless_it_changes(monkeypatch):
    task = {"type": "needs_vote", "target_id": "p1", "title": "Vote", "reason": "open"}
    _install_client(monkeypatch, tasks=[task])
    assert "p1" in await digest.gather_community_digest(_cfg())
    # The platform lists it again on the next tick; the agent is not asked again.
    assert await digest.gather_community_digest(_cfg()) == ""
    # It changed (a new head, a new update): presented again.
    updated = {**task, "updated_at": "2026-10-26T10:00:00+00:00"}
    _install_client(monkeypatch, tasks=[updated])
    assert "p1" in await digest.gather_community_digest(_cfg())
    # Another task is new even while the first is quiet.
    _install_client(monkeypatch, tasks=[updated, {**task, "target_id": "p2"}])
    out = await digest.gather_community_digest(_cfg())
    assert "p2" in out and "p1" not in out


async def test_the_feed_fallback_is_presented_once_too(monkeypatch):
    _install_client(
        monkeypatch,
        tasks_raises=client_mod.CommunityError("HTTP 404"),
        feed=[{"id": "p1", "status": "open", "title": "Anything", "author_handle": "a"}],
    )
    assert "p1" in await digest.gather_community_digest(_cfg())
    assert await digest.gather_community_digest(_cfg()) == ""


def test_fresh_items_repeat_after_the_quiet_period_and_forget_what_is_gone():
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)

    def key(item):
        return item["id"]

    assert digest.fresh_items([{"id": "a"}, {"id": "b"}], key, now=now) == [{"id": "a"}, {"id": "b"}]
    assert digest.fresh_items([{"id": "a"}], key, now=now + timedelta(hours=1)) == []
    # "b" was not listed in the previous call, so it is new again when it returns.
    assert digest.fresh_items([{"id": "a"}, {"id": "b"}], key, now=now + timedelta(hours=2)) == [{"id": "b"}]
    # After the quiet period an unchanged item is presented again.
    assert digest.fresh_items([{"id": "a"}], key, now=now + timedelta(hours=25)) == [{"id": "a"}]


def test_fresh_items_treat_an_unreadable_record_as_empty(tmp_path):
    path = digest.seen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert digest.fresh_items([{"id": "a"}], lambda item: item["id"]) == [{"id": "a"}]
    # And the record is usable afterwards.
    assert digest.fresh_items([{"id": "a"}], lambda item: item["id"]) == []
