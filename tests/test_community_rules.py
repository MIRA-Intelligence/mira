"""Tests for client-side community rules sync (#33).

Covers the onboarding=acceptance model on the engine side: rules delivered by
the server are cached on ``config.community`` (version + a rendered block),
persisted, and silently acknowledged — never a hard gate.
"""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.community import rules as rules_mod
from mira_engine.community.rules import render_rules_text, sync_community_rules


def _community(**kw):
    base = dict(
        api_base="http://x/community",
        agent_token="tok",
        rules_version=0,
        rules_text="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_render_marks_enforced_rules():
    out = render_rules_text(
        1,
        [
            {"title": "No secrets", "text": "don't leak", "enforcement": "hard"},
            {"title": "Be kind", "text": "please", "enforcement": "soft"},
        ],
    )
    assert "v1" in out
    assert "[enforced] No secrets: don't leak" in out
    assert "Be kind: please" in out
    assert "[enforced] Be kind" not in out


def test_extract_handles_both_shapes():
    assert rules_mod._extract({"version": 2, "rules": [{"a": 1}]}) == (2, [{"a": 1}])
    assert rules_mod._extract({"version": 3, "items": [{"b": 2}]}) == (3, [{"b": 2}])
    assert rules_mod._extract({"version": None}) == (None, [])
    assert rules_mod._extract({}) == (None, [])


async def test_sync_fetches_caches_acks_and_persists(tmp_path):
    cfg_path = tmp_path / "config.json"
    community = _community()
    acked: list[int] = []

    class FakeClient:
        async def get_rules(self):
            return {
                "version": 4,
                "rules": [{"title": "R1", "text": "t1", "enforcement": "hard"}],
            }

        async def ack_rules(self, version):
            acked.append(version)
            return {"ok": True}

    changed = await sync_community_rules(
        community, client=FakeClient(), config_path=cfg_path
    )
    assert changed is True
    assert community.rules_version == 4
    assert "R1" in community.rules_text
    assert acked == [4]

    # Persisted to disk so a fresh engine start sees the cached rules.
    from mira_engine.config.loader import load_config

    reloaded = load_config(cfg_path)
    assert reloaded.community.rules_version == 4
    assert "R1" in reloaded.community.rules_text


async def test_sync_uses_delivered_payload_without_fetch(tmp_path):
    community = _community()
    acked: list[int] = []

    class FakeClient:
        async def get_rules(self):
            raise AssertionError("delivered payload must not trigger a fetch")

        async def ack_rules(self, version):
            acked.append(version)

    changed = await sync_community_rules(
        community,
        rules={"version": 2, "items": [{"title": "X", "text": "y"}]},
        client=FakeClient(),
        config_path=tmp_path / "c.json",
    )
    assert changed is True
    assert community.rules_version == 2
    assert acked == [2]


async def test_sync_no_change_returns_false_and_skips_ack(tmp_path):
    text = render_rules_text(2, [{"title": "X", "text": "y"}])
    community = _community(rules_version=2, rules_text=text)

    class FakeClient:
        async def ack_rules(self, version):
            raise AssertionError("no re-ack when nothing changed")

    changed = await sync_community_rules(
        community,
        rules={"version": 2, "items": [{"title": "X", "text": "y"}]},
        client=FakeClient(),
        config_path=tmp_path / "c.json",
    )
    assert changed is False


async def test_sync_returns_false_when_unconfigured():
    community = _community(api_base="", agent_token="")
    assert await sync_community_rules(community) is False


async def test_sync_never_raises_on_client_error(tmp_path, monkeypatch):
    community = _community()
    monkeypatch.setattr(rules_mod, "_persist", lambda *a, **k: None)

    class BoomClient:
        async def get_rules(self):
            raise RuntimeError("network down")

    # Must swallow the error and report "no change".
    assert await sync_community_rules(community, client=BoomClient()) is False
