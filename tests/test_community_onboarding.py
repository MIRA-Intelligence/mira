"""Tests for manual community onboarding (#33)."""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.community import rules as rules_mod
from mira_engine.community.onboarding import onboard

_UUID = "24110f35-3c7d-4006-81e4-02a54584a7a4"


def _cfg(**kw):
    base = dict(
        api_base="http://x/community",
        agent_token="tok",
        rules_version=0,
        rules_text="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    def __init__(self, rules_doc, comment_res, *, status="pending", comments=None):
        self._rules_doc = rules_doc
        self._comment_res = comment_res
        self._status = status
        self._comments = comments or []
        self.calls: list = []

    async def get_me(self):
        return {"id": "agent-1", "name": "Tester", "status": self._status}

    async def get_proposal(self, proposal_id):
        return {"comments": self._comments}

    async def get_rules(self):
        return self._rules_doc

    async def post_comment(self, thread_id, content, reply_to=None):
        self.calls.append(("post_comment", thread_id, content))
        return self._comment_res

    async def ack_rules(self, version):
        self.calls.append(("ack_rules", version))
        return {"ok": True}


def _posted(client):
    return [c for c in client.calls if c[0] == "post_comment"]


async def test_onboard_completes_and_caches_rules(monkeypatch):
    monkeypatch.setattr(rules_mod, "_persist", lambda *a, **k: None)
    cfg = _cfg()
    client = FakeClient(
        {"onboarding_thread_id": _UUID, "version": 2},
        {"comment_id": "c1", "rules": {"version": 2, "items": [{"title": "R", "text": "t"}]}},
    )
    res = await onboard(cfg, client=client)
    assert res["ok"] is True
    assert res["onboarded"] is True
    assert res["rules_version"] == 2
    assert cfg.rules_version == 2
    assert "R" in cfg.rules_text
    assert any(c[0] == "post_comment" and c[1] == _UUID for c in client.calls)


async def test_onboard_already_active_returns_not_onboarded():
    cfg = _cfg()
    client = FakeClient({"onboarding_thread_id": _UUID, "version": 2}, {"comment_id": "c2"})
    res = await onboard(cfg, client=client)
    assert res["ok"] is True
    assert res["onboarded"] is False


async def test_onboard_not_logged_in():
    res = await onboard(SimpleNamespace(api_base="", agent_token=""))
    assert res["ok"] is False
    assert "logged in" in res["error"]


async def test_onboard_without_thread_configured():
    cfg = _cfg()
    client = FakeClient({"version": 2}, {})
    res = await onboard(cfg, client=client)
    assert res["ok"] is False
    assert "onboarding thread" in res["error"]


async def test_onboard_surfaces_rule_denial():
    from mira_engine.community.client import CommunityRuleError

    cfg = _cfg()

    class Blocked(FakeClient):
        async def post_comment(self, thread_id, content, reply_to=None):
            raise CommunityRuleError(
                "blocked",
                rule_id="suspended",
                reason="account suspended",
                how_to_resolve="contact a maintainer",
                status_code=403,
            )

    client = Blocked({"onboarding_thread_id": _UUID, "version": 2}, {})
    res = await onboard(cfg, client=client)
    assert res["ok"] is False
    assert res["rule_id"] == "suspended"


async def test_onboard_uses_custom_message():
    cfg = _cfg()
    client = FakeClient({"onboarding_thread_id": _UUID, "version": 2}, {"comment_id": "c3"})
    await onboard(cfg, message="custom hi", client=client)
    assert ("post_comment", _UUID, "custom hi") in client.calls


async def test_onboard_skips_when_already_active():
    cfg = _cfg()
    client = FakeClient(
        {"onboarding_thread_id": _UUID, "version": 2}, {"comment_id": "x"}, status="active"
    )
    res = await onboard(cfg, client=client)
    assert res["ok"] is True
    assert res["already"] is True
    assert res["status"] == "active"
    assert _posted(client) == []  # no duplicate reply


async def test_onboard_skips_when_already_replied_by_id():
    cfg = _cfg()
    client = FakeClient(
        {"onboarding_thread_id": _UUID, "version": 2},
        {"comment_id": "x"},
        status="pending",
        comments=[{"author_agent_id": "agent-1", "body": "hi"}],
    )
    res = await onboard(cfg, client=client)
    assert res["already"] is True
    assert _posted(client) == []


async def test_onboard_skips_when_already_replied_by_name():
    cfg = _cfg()
    client = FakeClient(
        {"onboarding_thread_id": _UUID, "version": 2},
        {"comment_id": "x"},
        status="pending",
        comments=[{"author_name": "Tester", "body": "hi"}],
    )
    res = await onboard(cfg, client=client)
    assert res["already"] is True
    assert _posted(client) == []


async def test_onboard_force_posts_even_if_active():
    cfg = _cfg()
    client = FakeClient(
        {"onboarding_thread_id": _UUID, "version": 2}, {"comment_id": "x"}, status="active"
    )
    res = await onboard(cfg, client=client, force=True)
    assert res["ok"] is True
    assert len(_posted(client)) == 1
