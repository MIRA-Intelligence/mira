"""Tests for the community channel's event handling (#33).

Focus: lifecycle events (rules_updated / onboarded) are intercepted to cache
rules client-side and must NOT spawn an agent turn, while ordinary events still
publish an inbound message.
"""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.channels.community import CommunityChannel, _event_thread_id
from mira_engine.community import rules as rules_mod

_UUID = "24110f35-3c7d-4006-81e4-02a54584a7a4"
_UUID2 = "11111111-1111-4111-8111-111111111111"


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish_inbound(self, msg) -> None:
        self.published.append(msg)


def _channel() -> tuple[CommunityChannel, _FakeBus]:
    bus = _FakeBus()
    config = SimpleNamespace(
        api_base="http://x/community",
        agent_token="tok",
        agent_id="a1",
        rules_version=0,
        rules_text="",
    )
    return CommunityChannel(config, bus), bus


def test_read_credentials_from_disk(tmp_path, monkeypatch):
    import json

    from mira_engine.config import loader

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {"community": {"apiBase": "https://x/community/", "agentToken": "T2", "agentId": "id2"}}
        )
    )
    monkeypatch.setattr(loader, "get_config_path", lambda: cfg_file)
    channel, _ = _channel()
    assert channel._read_credentials() == ("https://x/community", "T2", "id2")


def test_read_credentials_falls_back_to_snapshot(tmp_path, monkeypatch):
    from mira_engine.config import loader

    missing = tmp_path / "nope.json"
    monkeypatch.setattr(loader, "get_config_path", lambda: missing)
    channel, _ = _channel()
    # No file on disk → use the construction snapshot.
    assert channel._read_credentials() == ("http://x/community", "tok", "a1")


def test_read_credentials_logged_out(tmp_path, monkeypatch):
    import json

    from mira_engine.config import loader

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"community": {"apiBase": "https://x/community", "agentToken": ""}}))
    monkeypatch.setattr(loader, "get_config_path", lambda: cfg_file)
    channel, _ = _channel()
    _, token, _ = channel._read_credentials()
    assert token == ""


async def test_rules_updated_event_caches_rules_without_turn(monkeypatch):
    channel, bus = _channel()
    calls: list[dict] = []

    async def fake_sync(community_config, *, rules=None, **kw):
        calls.append({"config": community_config, "rules": rules})
        return True

    monkeypatch.setattr(rules_mod, "sync_community_rules", fake_sync)

    await channel._handle_event(
        {"type": "rules_updated", "rules": {"version": 2, "items": [{"title": "R"}]}}
    )

    assert bus.published == []  # no agent turn spawned
    assert len(calls) == 1
    assert calls[0]["rules"] == {"version": 2, "items": [{"title": "R"}]}


async def test_onboarded_event_caches_rules_without_turn(monkeypatch):
    channel, bus = _channel()
    calls: list[dict] = []

    async def fake_sync(community_config, *, rules=None, **kw):
        calls.append(rules)
        return True

    monkeypatch.setattr(rules_mod, "sync_community_rules", fake_sync)

    await channel._handle_event(
        {"type": "onboarded", "rules": {"version": 1, "items": []}}
    )

    assert bus.published == []
    assert calls == [{"version": 1, "items": []}]


def test_event_thread_id_prefers_action_body_thread():
    # Onboarding/mention tasks carry the thread in action.body.thread_id; the
    # event row id must NOT be used (that was the dropped-reply bug).
    event = {
        "type": "onboarding",
        "id": 2,
        "target_id": _UUID,
        "action": {"method": "POST", "path": "/agents/messages", "body": {"thread_id": _UUID}},
    }
    assert _event_thread_id(event) == _UUID


def test_event_thread_id_uses_proposal_id_for_votes():
    event = {
        "type": "needs_vote",
        "id": 7,
        "target_id": _UUID,
        "action": {"method": "POST", "path": "/agents/votes", "body": {"proposal_id": _UUID, "value": 1}},
    }
    assert _event_thread_id(event) == _UUID


def test_event_thread_id_falls_back_to_target_then_sentinel():
    assert _event_thread_id({"type": "x", "id": 9, "target_id": _UUID2}) == _UUID2
    # No UUID anywhere -> sentinel (never the bigint row id).
    assert _event_thread_id({"type": "x", "id": 9, "target_id": "rules:2"}) == "community"
    assert _event_thread_id({"type": "ready"}) == "community"


async def test_onboarding_event_routes_reply_to_thread(monkeypatch):
    channel, bus = _channel()

    async def no_sync(*a, **k):
        return False

    monkeypatch.setattr(rules_mod, "sync_community_rules", no_sync)

    await channel._handle_event(
        {
            "type": "onboarding",
            "id": 3,
            "target_id": _UUID,
            "title": "Complete your connection test",
            "action": {"method": "POST", "path": "/agents/messages", "body": {"thread_id": _UUID}},
        }
    )
    assert len(bus.published) == 1
    # The inbound message's chat_id is the onboarding thread UUID, so the agent's
    # reply lands on the right thread (not the event row id).
    assert bus.published[0].chat_id == _UUID


async def test_ordinary_event_still_publishes(monkeypatch):
    channel, bus = _channel()

    async def boom(*a, **k):
        raise AssertionError("ordinary events must not trigger rules sync")

    monkeypatch.setattr(rules_mod, "sync_community_rules", boom)

    await channel._handle_event(
        {
            "type": "mention",
            "thread_id": "11111111-1111-4111-8111-111111111111",
            "content": "hey @you",
            "actor": "other",
        }
    )

    assert len(bus.published) == 1
    assert bus.published[0].channel == "community"
