"""After posting a development proposal the agent offers to start building it."""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.agent.tools import community as community_tools
from mira_engine.community import approvals, contribute
from mira_engine.community.autonomy import AutonomyGate

ME = "a1b2c3d4-0000-4000-8000-000000000001"
OTHER = "f0e1d2c3-0000-4000-8000-000000000002"


class _Client:
    def __init__(self, status="building", claimed_by=ME, category="development", claim_error=None):
        self.calls = []
        self.proposal = {"id": "p1", "title": "Local model picker", "category": category,
                         "status": status, "claimed_by_agent_id": claimed_by, "claimed_by_name": "Nova"}
        self.claim_error = claim_error

    async def create_proposal(self, title, body, category="development", tags=None):
        self.calls.append(("create_proposal", title))
        return {"id": "p1", "category": category}

    async def get_me(self):
        return {"id": ME, "web_url": "http://web.test/"}

    async def get_proposal(self, proposal_id):
        return {"proposal": self.proposal, "comments": [], "prs": []}

    async def claim_proposal(self, proposal_id):
        self.calls.append(("claim_proposal", proposal_id))
        if self.claim_error:
            raise self.claim_error
        return {"claim": {"proposal_id": proposal_id, "status": "building", "claim_expires_at": "2026-10-23T12:00:00Z"}}

    async def release_proposal(self, proposal_id):
        self.calls.append(("release_proposal", proposal_id))
        return {"claim": {}}

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
    assert "community_claim_proposal with proposal_id='p1'" in out and "Ask first" in out


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


# ------------------------------------------------------------------ claims


def _claim_tool(client, mode="fully_autonomous"):
    return community_tools.CommunityClaimProposalTool(client, AutonomyGate(mode), SimpleNamespace())


async def test_an_open_proposal_is_claimed_and_the_agent_is_told_what_next():
    client = _Client(status="open", claimed_by=None)
    out = await _claim_tool(client).execute(proposal_id="p1")
    assert ("claim_proposal", "p1") in client.calls
    assert 'Claimed "Local model picker"' in out and "2026-10-23" in out
    assert "community_start_contribution with proposal_id='p1'" in out and "community_proposal_lease" in out


async def test_a_proposal_someone_else_is_building_is_not_claimed():
    client = _Client(status="building", claimed_by=OTHER)
    out = await _claim_tool(client).execute(proposal_id="p1")
    assert out.startswith("Not claiming") and "Nova #f0e1d2c3" in out
    assert ("claim_proposal", "p1") not in client.calls


async def test_only_open_development_proposals_are_claimed():
    for client in (_Client(status="merged", claimed_by=None), _Client(status="open", category="question")):
        out = await _claim_tool(client).execute(proposal_id="p1")
        assert out.startswith("Not claiming") and not client.calls


async def test_your_own_claim_is_renewed():
    client = _Client(status="building", claimed_by=ME)
    assert "Claimed" in await _claim_tool(client).execute(proposal_id="p1")
    assert ("claim_proposal", "p1") in client.calls


async def test_losing_a_race_reports_the_platforms_reason():
    from mira_engine.community.client import CommunityRuleError

    err = CommunityRuleError("x", rule_id="proposal-claimed", reason="Ada #12345678 is building it.", status_code=409)
    out = await _claim_tool(_Client(status="open", claimed_by=None, claim_error=err)).execute(proposal_id="p1")
    assert out == "Not claimed: Ada #12345678 is building it."


async def test_a_held_claim_remembers_the_conversation(monkeypatch):
    tool = _claim_tool(_Client(status="open", claimed_by=None), mode="hybrid")
    queued = []
    monkeypatch.setattr(tool._gate, "enqueue_approval", lambda a, p, o=None: queued.append((a, p, o)) or "a1")
    tool.set_context("ui", "chat-7")
    out = await tool.execute(proposal_id="p1")
    assert queued == [("claim_proposal", {"proposal_id": "p1", "title": "Local model picker"}, {"channel": "ui", "chat_id": "chat-7"})]
    assert "you will be told in this conversation" in out


async def test_renewing_and_releasing_a_claim():
    client = _Client()
    tool = community_tools.CommunityProposalLeaseTool(client, AutonomyGate("hybrid"))
    assert "renewed until 2026-10-23" in await tool.execute(proposal_id="p1", action="renew")
    assert "open for others again" in await tool.execute(proposal_id="p1", action="release")
    assert client.calls == [("claim_proposal", "p1"), ("release_proposal", "p1")]
    assert "must be renew or release" in await tool.execute(proposal_id="p1", action="drop")


async def test_building_needs_your_claim(monkeypatch, tmp_path):
    started = []

    async def fake_start(**kw):
        started.append(kw)

    monkeypatch.setattr(contribute, "start_contribution", fake_start)
    for client, expected in (
        (_Client(status="open", claimed_by=None), "claim the proposal first"),
        (_Client(status="building", claimed_by=OTHER), "Nova #f0e1d2c3 is building"),
    ):
        tool = community_tools.CommunityStartContributionTool(client, AutonomyGate("hitl"), SimpleNamespace(), tmp_path)
        out = await tool.execute(repo="o/r", proposal_id="p1")
        assert out.startswith("Not starting") and expected in out
    assert not started


async def test_approved_claims_run(monkeypatch):
    calls = []

    async def claim(self, proposal_id):
        calls.append(("claim", proposal_id))
        return {"claim": {"claim_expires_at": "2026-10-23T12:00:00Z"}}

    async def release(self, proposal_id):
        calls.append(("release", proposal_id))
        return {}

    monkeypatch.setattr("mira_engine.community.client.CommunityClient.claim_proposal", claim)
    monkeypatch.setattr("mira_engine.community.client.CommunityClient.release_proposal", release)
    cfg = SimpleNamespace(api_base="http://x", agent_token="t")
    for action in ("claim_proposal", "renew_proposal_claim", "release_proposal_claim"):
        res = await approvals.execute_approval({"action": action, "payload": {"proposal_id": "p1"}}, cfg)
        assert res["ok"], res
    assert calls == [("claim", "p1"), ("claim", "p1"), ("release", "p1")]


def test_claim_and_renew_tiers():
    hybrid = AutonomyGate("hybrid")
    assert hybrid.requires_approval("claim_proposal")
    assert not hybrid.requires_approval("renew_proposal_claim") and not hybrid.requires_approval("release_proposal_claim")


def test_claim_events_reach_the_inbox_as_work_and_prompt_the_agent():
    from mira_engine.channels.community import CommunityChannel
    from mira_engine.community import inbox

    base = {"type": "proposal_claim", "proposal_id": "p1", "title": "Picker"}
    expiring = {**base, "state": "expiring", "expires_at": "2026-10-22T10:00:00Z", "has_pr": True}
    expired = {**base, "state": "expired", "has_pr": True}
    closed = {**base, "state": "pr_closed", "expires_at": "2026-10-22T10:00:00Z"}
    rejected = {**base, "state": "rejected", "score": -3}
    assert {inbox.kind_of(e) for e in (expiring, expired, closed, rejected)} == {"work"}
    assert inbox._title(expiring) == 'Your claim on "Picker" expires soon'
    assert inbox._title(expired) == 'Your claim on "Picker" expired; it is open to others again'
    assert "voted" in inbox._title(rejected) and "still hold the claim" in inbox._title(closed)

    text = CommunityChannel._format_event(expiring)
    assert "and your PR for it lapses at 2026-10-22T10:00:00Z" in text
    assert "action='renew'" in text and "Community inbox" in text and "community_comment" not in text
    assert "your PR can't merge unless you claim it again" in CommunityChannel._format_event(expired)
    assert "score -3" in CommunityChannel._format_event(rejected)
    assert "until 2026-10-22T10:00:00Z" in CommunityChannel._format_event(closed)


def test_each_lease_gets_its_own_reminder(tmp_path, monkeypatch):
    from mira_engine.community import inbox

    monkeypatch.setattr("mira_engine.community.inbox.get_data_dir", lambda: tmp_path)
    first = {"type": "proposal_claim", "target_id": "p1", "proposal_id": "p1", "state": "expiring", "expires_at": "A"}
    item = inbox.record_event(first, ["work"])
    assert item and item["expires_at"] == "A" and item["thread_id"] == "p1"
    assert inbox.record_event(first, ["work"]) is None
    assert inbox.record_event({**first, "expires_at": "B"}, ["work"]) is not None
