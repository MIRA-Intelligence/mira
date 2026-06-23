"""Tests for manual community onboarding decision logic (#33)."""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.community.onboarding import check_onboarding_needed

_UUID = "24110f35-3c7d-4006-81e4-02a54584a7a4"


def _cfg(**kw):
    base = dict(api_base="http://x/community", agent_token="tok", agent_id="agent-1")
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    def __init__(self, *, status="pending", thread_id=_UUID, comments=None):
        self._status = status
        self._thread_id = thread_id
        self._comments = comments or []
        self.calls: list = []

    async def get_me(self):
        self.calls.append(("get_me",))
        return {"id": "agent-1", "name": "Tester", "status": self._status}

    async def get_rules(self):
        self.calls.append(("get_rules",))
        return {"onboarding_thread_id": self._thread_id, "version": 2}

    async def get_proposal(self, proposal_id):
        self.calls.append(("get_proposal", proposal_id))
        return {"comments": self._comments}


async def test_needed_when_pending_and_no_reply():
    res = await check_onboarding_needed(_cfg(), client=FakeClient(status="pending"))
    assert res["needed"] is True
    assert res["thread_id"] == _UUID
    assert res["status"] == "pending"


async def test_skips_when_already_active():
    res = await check_onboarding_needed(_cfg(), client=FakeClient(status="active"))
    assert res["needed"] is False
    assert res["already"] is True
    assert res["status"] == "active"


async def test_skips_when_already_replied_by_id():
    client = FakeClient(status="pending", comments=[{"author_agent_id": "agent-1"}])
    res = await check_onboarding_needed(_cfg(), client=client)
    assert res["needed"] is False
    assert res["already"] is True


async def test_skips_when_already_replied_by_name():
    client = FakeClient(status="pending", comments=[{"author_name": "Tester"}])
    res = await check_onboarding_needed(_cfg(), client=client)
    assert res["needed"] is False
    assert res["already"] is True


async def test_not_logged_in():
    res = await check_onboarding_needed(SimpleNamespace(api_base="", agent_token=""))
    assert res["needed"] is False
    assert "logged in" in res["error"]


async def test_no_thread_configured():
    res = await check_onboarding_needed(_cfg(), client=FakeClient(thread_id=None))
    assert res["needed"] is False
    assert "onboarding thread" in res["error"]
