"""Tests for the community approvals store + executor (#114)."""

from types import SimpleNamespace

import pytest

from mira_engine.community import approvals
from mira_engine.community import client as client_mod


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "get_data_dir", lambda: tmp_path)
    return tmp_path


def test_append_and_list(data_dir):
    i1 = approvals.append_approval("comment", {"thread_id": "t1", "content": "hi"})
    i2 = approvals.append_approval("post_proposal", {"title": "T", "body": "B"})
    items = approvals.list_approvals()
    assert {x["id"] for x in items} == {i1, i2}
    assert all(x["status"] == "pending" for x in items)
    assert len(approvals.list_approvals(status="pending")) == 2


def test_set_status_updates_and_supersedes(data_dir):
    i1 = approvals.append_approval("comment", {"x": 1})
    rec = approvals.set_status(i1, "rejected")
    assert rec is not None
    assert rec["status"] == "rejected"
    assert "decided_at" in rec
    assert approvals.list_approvals(status="pending") == []
    assert approvals.get_approval(i1)["status"] == "rejected"


def test_set_status_unknown_returns_none(data_dir):
    assert approvals.set_status("does-not-exist", "approved") is None


def test_list_empty_when_no_file(data_dir):
    assert approvals.list_approvals() == []


async def test_execute_proposal_calls_client(monkeypatch, data_dir):
    calls: dict = {}

    class FakeClient:
        def __init__(self, base, token):
            calls["init"] = (base, token)

        async def create_proposal(self, title, body):
            calls["proposal"] = (title, body)
            return {"id": "p1"}

        async def post_comment(self, *a, **k):  # pragma: no cover - unused here
            raise AssertionError("should not be called")

    monkeypatch.setattr(client_mod, "CommunityClient", FakeClient)
    rec = {"id": "1", "action": "post_proposal", "payload": {"title": "T", "body": "B"}}
    cfg = SimpleNamespace(api_base="http://x", agent_token="tok")
    res = await approvals.execute_approval(rec, cfg)
    assert res["ok"] is True
    assert calls["proposal"] == ("T", "B")
    assert calls["init"] == ("http://x", "tok")


async def test_execute_requires_connection(data_dir):
    rec = {"id": "1", "action": "comment", "payload": {}}
    cfg = SimpleNamespace(api_base="", agent_token="")
    res = await approvals.execute_approval(rec, cfg)
    assert res["ok"] is False


async def test_execute_unsupported_action(monkeypatch, data_dir):
    class FakeClient:
        def __init__(self, *a):
            pass

    monkeypatch.setattr(client_mod, "CommunityClient", FakeClient)
    rec = {"id": "1", "action": "draft_patch", "payload": {}}
    cfg = SimpleNamespace(api_base="http://x", agent_token="t")
    res = await approvals.execute_approval(rec, cfg)
    assert res["ok"] is False
    assert "unsupported" in res["detail"]


async def test_execute_vote_calls_client(monkeypatch, data_dir):
    calls: dict = {}

    class FakeClient:
        def __init__(self, base, token):
            calls["init"] = (base, token)

        async def vote(self, proposal_id, value):
            calls["vote"] = (proposal_id, value)
            return {"score": 5}

    monkeypatch.setattr(client_mod, "CommunityClient", FakeClient)
    rec = {"id": "1", "action": "vote", "payload": {"proposal_id": "p9", "value": 1}}
    cfg = SimpleNamespace(api_base="http://x", agent_token="tok")
    res = await approvals.execute_approval(rec, cfg)
    assert res["ok"] is True
    assert calls["vote"] == ("p9", 1)


async def test_execute_submit_patch_calls_client(monkeypatch, data_dir):
    calls: dict = {}

    class FakeClient:
        def __init__(self, base, token):
            calls["init"] = (base, token)

        async def submit_patch(self, proposal_id, repo, diff, title, body="", base_ref="main"):
            calls["patch"] = (proposal_id, repo, title, base_ref)
            return {"id": "patch9"}

    monkeypatch.setattr(client_mod, "CommunityClient", FakeClient)
    rec = {
        "id": "1",
        "action": "submit_patch",
        "payload": {
            "proposal_id": "p1",
            "repo": "o/r",
            "diff": "d",
            "title": "Fix",
            "base_ref": "main",
        },
    }
    cfg = SimpleNamespace(api_base="http://x", agent_token="tok")
    res = await approvals.execute_approval(rec, cfg)
    assert res["ok"] is True
    assert calls["patch"] == ("p1", "o/r", "Fix", "main")
