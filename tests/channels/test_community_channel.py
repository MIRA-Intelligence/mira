"""Tests for the community channel's event handling (#33).

Focus: lifecycle events (rules_updated / onboarded) are intercepted to cache
rules client-side and must NOT spawn an agent turn, while ordinary events still
publish an inbound message.
"""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.channels.community import CommunityChannel
from mira_engine.community import rules as rules_mod


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
