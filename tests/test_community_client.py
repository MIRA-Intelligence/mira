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
