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

    async def submit_patch(self, proposal_id, repo, diff, title, body="", base_ref="main"):
        self.calls.append(("submit_patch", proposal_id, repo, title, base_ref))
        return {"id": "patch1"}

    async def get_proposal(self, proposal_id):
        self.calls.append(("get_proposal", proposal_id))
        return {
            "proposal": {"title": "Dark mode", "status": "building"},
            "prs": [
                {
                    "host": "github",
                    "repo": "o/r",
                    "pr_number": 7,
                    "status": "open",
                    "ci_state": "passed",
                    "url": "https://x/7",
                }
            ],
        }


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
        "community_draft_patch",
        "community_submit_patch",
        "community_review_pr",
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


async def test_draft_patch_validates_without_network():
    client = FakeClient()
    tool = community_tools.CommunityDraftPatchTool(client, AutonomyGate("hitl"))
    good = await tool.execute(diff="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    assert "valid" in good.lower()
    bad = await tool.execute(diff="just some text")
    assert "does not look like" in bad.lower()
    assert client.calls == []


async def test_submit_patch_runs_when_autonomous():
    client = FakeClient()
    tool = community_tools.CommunitySubmitPatchTool(client, AutonomyGate("fully_autonomous"))
    out = await tool.execute(
        proposal_id="p1", repo="o/r", diff="diff --git a/x b/x\n", title="Fix"
    )
    assert "submitted" in out.lower()
    assert ("submit_patch", "p1", "o/r", "Fix", "main") in client.calls


async def test_submit_patch_queued_in_hybrid(monkeypatch):
    client = FakeClient()
    gate = AutonomyGate("hybrid")
    monkeypatch.setattr(gate, "enqueue_approval", lambda a, p: "appr-3")
    tool = community_tools.CommunitySubmitPatchTool(client, gate)
    out = await tool.execute(proposal_id="p1", repo="o/r", diff="d", title="Fix")
    assert "approval" in out.lower()
    assert client.calls == []


async def test_review_pr_lists_prs():
    client = FakeClient()
    tool = community_tools.CommunityReviewPrTool(client, AutonomyGate("hitl"))
    out = await tool.execute(proposal_id="p1")
    assert "Dark mode" in out
    assert "github" in out
    assert "passed" in out
