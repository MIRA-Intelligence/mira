"""Tests for the community client's structured error handling (#13)."""

import httpx
import pytest

from mira_engine.community.client import (
    CommunityClient,
    CommunityError,
    CommunityRuleError,
)


def test_raise_for_error_structured_rule():
    resp = httpx.Response(
        403,
        json={
            "error": "onboarding required",
            "rule_id": "onboarding",
            "reason": "complete the connection test",
            "how_to_resolve": "reply in the onboarding thread",
        },
    )
    with pytest.raises(CommunityRuleError) as ei:
        CommunityClient._raise_for_error("/agents/messages", resp)
    err = ei.value
    assert err.rule_id == "onboarding"
    assert err.how_to_resolve == "reply in the onboarding thread"
    assert err.status_code == 403


def test_raise_for_error_plain():
    resp = httpx.Response(500, text="boom")
    with pytest.raises(CommunityError) as ei:
        CommunityClient._raise_for_error("/agents/messages", resp)
    assert not isinstance(ei.value, CommunityRuleError)


async def test_register_pr_payload():
    client = CommunityClient("http://x", "tok")
    captured: dict = {}

    async def fake_post(path, json):
        captured["path"] = path
        captured["json"] = json
        return {"pr": {"id": "1", "state": "open"}}

    client._post = fake_post  # type: ignore[assignment]
    await client.register_pr("p1", "o/r", 5, "http://u/pull/5", "alice")
    assert captured["path"] == "/agents/prs"
    assert captured["json"] == {
        "proposal_id": "p1",
        "host": "github",
        "repo": "o/r",
        "pr_number": 5,
        "url": "http://u/pull/5",
        "author_login": "alice",
    }


async def test_get_me_uses_auth_get():
    client = CommunityClient("http://x", "tok")
    captured: dict = {}

    async def fake_get(path, params=None, *, auth=False):
        captured["path"] = path
        captured["auth"] = auth
        return {"tier": "recruit"}

    client._get = fake_get  # type: ignore[assignment]
    res = await client.get_me()
    assert captured == {"path": "/agents/me", "auth": True}
    assert res["tier"] == "recruit"
