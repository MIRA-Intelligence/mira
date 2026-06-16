"""Tests for the community_* agent tools + the build factory (#112)."""

from types import SimpleNamespace

from mira_engine.agent.tools import community as community_tools
from mira_engine.community.autonomy import AutonomyGate


class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    async def read_feed(self, limit=20, status=None):
        self.calls.append(("read_feed", limit, status))
        return {
            "proposals": [
                {
                    "id": "p1",
                    "status": "open",
                    "title": "Add dark mode",
                    "score": 3,
                    "comment_count": 2,
                    "author_handle": "alice",
                }
            ]
        }

    async def create_proposal(self, title, body):
        self.calls.append(("create_proposal", title, body))
        return {"id": "p2"}

    async def post_comment(self, thread_id, content, reply_to=None):
        self.calls.append(("post_comment", thread_id, content, reply_to))
        return {"comment_id": "c1"}

    async def vote(self, proposal_id, value):
        self.calls.append(("vote", proposal_id, value))
        return {"score": 4}


def _cfg(**kw):
    base = dict(
        enabled=True,
        api_base="http://x/community",
        agent_token="tok",
        autonomy_mode="hitl",
        domains=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_returns_empty_when_disabled():
    assert community_tools.build_community_tools(_cfg(enabled=False)) == []
    assert community_tools.build_community_tools(_cfg(agent_token="")) == []
    assert community_tools.build_community_tools(None) == []


def test_build_returns_full_set_when_connected():
    tools = community_tools.build_community_tools(_cfg(autonomy_mode="fully_autonomous"))
    names = {t.name for t in tools}
    assert names == {
        "community_read_feed",
        "community_post_proposal",
        "community_comment",
        "community_vote",
    }


async def test_read_feed_formats_proposals():
    client = FakeClient()
    tool = community_tools.CommunityReadFeedTool(client, AutonomyGate("hitl"))
    out = await tool.execute(limit=5)
    assert "Add dark mode" in out
    assert "p1" in out
    assert client.calls[0] == ("read_feed", 5, None)


async def test_vote_runs_when_fully_autonomous():
    client = FakeClient()
    tool = community_tools.CommunityVoteTool(client, AutonomyGate("fully_autonomous"))
    out = await tool.execute(proposal_id="p1", value=1)
    assert "Vote recorded" in out
    assert ("vote", "p1", 1) in client.calls


async def test_vote_queued_in_hitl(monkeypatch):
    queued: dict = {}

    def fake_enqueue(action, payload):
        queued["action"] = action
        queued["payload"] = payload
        return "appr-1"

    client = FakeClient()
    gate = AutonomyGate("hitl")
    monkeypatch.setattr(gate, "enqueue_approval", fake_enqueue)
    tool = community_tools.CommunityVoteTool(client, gate)
    out = await tool.execute(proposal_id="p1", value=-1)
    assert "approval" in out.lower()
    assert queued["action"] == "vote"
    assert queued["payload"] == {"proposal_id": "p1", "value": -1}
    # nothing should have hit the network
    assert client.calls == []


async def test_comment_low_impact_runs_in_hybrid():
    client = FakeClient()
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("hybrid"))
    out = await tool.execute(thread_id="p1", content="nice")
    assert "Comment posted" in out
    assert ("post_comment", "p1", "nice", None) in client.calls


async def test_proposal_high_impact_queued_in_hybrid(monkeypatch):
    client = FakeClient()
    gate = AutonomyGate("hybrid")
    monkeypatch.setattr(gate, "enqueue_approval", lambda a, p: "appr-2")
    tool = community_tools.CommunityPostProposalTool(client, gate)
    out = await tool.execute(title="T", body="B")
    assert "approval" in out.lower()
    assert client.calls == []
