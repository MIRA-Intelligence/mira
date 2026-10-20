"""After posting a development proposal the agent offers to start building it."""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.agent.tools import community as community_tools
from mira_engine.community import approvals, contribute
from mira_engine.community.autonomy import AutonomyGate


class _Client:
    def __init__(self):
        self.calls = []

    async def create_proposal(self, title, body, category="development", tags=None):
        self.calls.append(("create_proposal", title))
        return {"id": "p1", "category": category}

    async def get_me(self):
        return {"web_url": "http://web.test/"}

    async def get_proposal(self, proposal_id):
        return {"id": proposal_id, "title": "Local model picker"}

    async def get_contribution(self):
        return {"landing_branch": "contrib", "lanes": {"plugin": {"paths": ["plugins/**"]}}}

    async def register_pr(self, proposal_id, repo, number, url, login, **kw):
        self.calls.append(("register_pr", proposal_id))
        return {"pr": {"id": "x1", "state": "open"}}


def _tool(mode, cfg=None):
    return community_tools.CommunityPostProposalTool(_Client(), AutonomyGate(mode), cfg or SimpleNamespace(web_url=""))


async def test_a_posted_proposal_tells_the_agent_to_offer_building_it():
    out = await _tool("fully_autonomous").execute(title="T", body="B")
    assert "p1" in out and "http://web.test/proposals/p1" in out
    assert "do not need to wait for votes" in out
    assert "community_start_contribution with proposal_id='p1'" in out and "Ask first" in out


async def test_the_configured_website_wins():
    out = await _tool("fully_autonomous", SimpleNamespace(web_url="https://c.example")).execute(title="T", body="B")
    assert "https://c.example/proposals/p1" in out


async def test_a_held_proposal_remembers_the_conversation(monkeypatch):
    tool = _tool("hitl")
    queued = []
    monkeypatch.setattr(tool._gate, "enqueue_approval", lambda a, p, o=None: queued.append((a, o)) or "a1")
    tool.set_context("ui", "chat-7")
    tool.set_project_context("PRJ-1", "/p")
    out = await tool.execute(title="T", body="B")
    assert queued == [("post_proposal", {"channel": "ui", "chat_id": "chat-7", "project_id": "PRJ-1"})]
    assert "you will be told in this conversation" in out


async def test_the_agents_own_community_turns_have_no_one_to_follow_up(monkeypatch):
    tool = _tool("hitl")
    queued = []
    monkeypatch.setattr(tool._gate, "enqueue_approval", lambda a, p, o=None: queued.append(o) or "a1")
    tool.set_context("community", "thread-1")
    await tool.execute(title="T", body="B")
    assert queued == [None]


def test_the_origin_is_stored_with_the_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "get_data_dir", lambda: tmp_path)
    aid = approvals.append_approval("post_proposal", {"title": "T"}, {"channel": "ui", "chat_id": "c"})
    assert approvals.get_approval(aid)["origin"] == {"channel": "ui", "chat_id": "c"}
    assert "origin" not in approvals.get_approval(approvals.append_approval("vote", {}))


async def test_executing_an_approved_proposal_returns_its_id(monkeypatch):
    async def create(self, title, body, category="development", tags=None):
        return {"id": "p9"}

    monkeypatch.setattr("mira_engine.community.client.CommunityClient.create_proposal", create)
    cfg = SimpleNamespace(api_base="http://x", agent_token="t")
    res = await approvals.execute_approval({"action": "post_proposal", "payload": {"title": "T"}}, cfg)
    assert res["ok"] and res["post_id"] == "p9"


async def test_building_a_proposal_links_the_pr_to_it(monkeypatch, tmp_path):
    seen = {}

    async def fake_start(**kw):
        seen.update(kw)
        return tmp_path / "ws", contribute.ContributionScope(
            repo=kw["repo"], landing_branch="contrib", branch="mira/x", proposal_id=kw["proposal_id"]
        )

    monkeypatch.setattr(contribute, "start_contribution", fake_start)
    client = _Client()
    tool = community_tools.CommunityStartContributionTool(client, AutonomyGate("hitl"), SimpleNamespace(), tmp_path)
    out = await tool.execute(repo="o/r", proposal_id="p1")
    assert seen["proposal_id"] == "p1" and seen["title"] == "Local model picker"
    assert "link to proposal p1" in out and "try the change locally" in out

    # community_open_pr links to the workspace's proposal without being told.
    async def fake_open(**kw):
        return contribute.OpenPrResult(url="https://github.com/o/r/pull/3", number=3, branch="mira/x", login="me")

    monkeypatch.setattr(contribute, "open_pr_github", fake_open)
    monkeypatch.setattr(contribute, "load_scope", lambda d: contribute.ContributionScope(
        repo="o/r", landing_branch="contrib", branch="mira/x", proposal_id="p1"))
    pr_tool = community_tools.CommunityOpenPrTool(client, AutonomyGate("fully_autonomous"))
    assert "Opened and registered" in await pr_tool.execute(working_dir=str(tmp_path / "ws"), title="T")
    assert ("register_pr", "p1") in client.calls


def test_an_old_scope_file_still_loads(tmp_path):
    import json

    (tmp_path / ".git").mkdir()
    contribute.scope_path(tmp_path).write_text(json.dumps({"repo": "o/r", "landing_branch": "contrib", "branch": "b"}))
    assert contribute.load_scope(tmp_path).proposal_id is None
