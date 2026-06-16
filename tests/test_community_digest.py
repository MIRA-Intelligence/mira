"""Tests for the heartbeat community digest (#112)."""

from types import SimpleNamespace

from mira_engine.community import client as client_mod
from mira_engine.community import digest


def _cfg(**kw):
    base = dict(
        enabled=True,
        api_base="http://x/community",
        agent_token="tok",
        domains=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _install_feed(monkeypatch, proposals):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def read_feed(self, limit=20, status=None):
            return {"proposals": proposals}

    monkeypatch.setattr(client_mod, "CommunityClient", FakeClient)


async def test_disabled_returns_empty():
    assert await digest.gather_community_digest(_cfg(enabled=False)) == ""
    assert await digest.gather_community_digest(_cfg(agent_token="")) == ""


async def test_empty_feed_returns_empty(monkeypatch):
    _install_feed(monkeypatch, [])
    assert await digest.gather_community_digest(_cfg()) == ""


async def test_no_domains_includes_all(monkeypatch):
    _install_feed(
        monkeypatch,
        [{"id": "p1", "status": "open", "title": "Anything", "author_handle": "a"}],
    )
    out = await digest.gather_community_digest(_cfg())
    assert "Anything" in out
    assert "p1" in out
    assert "Mira Community" in out


async def test_domain_filtering(monkeypatch):
    _install_feed(
        monkeypatch,
        [
            {"id": "p1", "status": "open", "title": "Improve chemistry tools", "author_handle": "a"},
            {"id": "p2", "status": "open", "title": "New billing page", "author_handle": "b"},
        ],
    )
    out = await digest.gather_community_digest(_cfg(domains=["chemistry"]))
    assert "chemistry" in out.lower()
    assert "p1" in out
    assert "p2" not in out


async def test_no_match_returns_empty(monkeypatch):
    _install_feed(
        monkeypatch,
        [{"id": "p1", "status": "open", "title": "Billing", "author_handle": "a"}],
    )
    assert await digest.gather_community_digest(_cfg(domains=["chemistry"])) == ""


async def test_network_error_returns_empty(monkeypatch):
    class BoomClient:
        def __init__(self, *a, **k):
            pass

        async def read_feed(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(client_mod, "CommunityClient", BoomClient)
    assert await digest.gather_community_digest(_cfg()) == ""
