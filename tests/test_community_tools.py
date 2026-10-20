"""Tests for the community_* agent tools + the build factory (#112)."""

from types import SimpleNamespace

from mira_engine.agent.tools import community as community_tools
from mira_engine.community.autonomy import AutonomyGate


class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    async def read_feed(self, limit=20, status=None, category=None, tag=None):
        self.calls.append(("read_feed", limit, status, category, tag))
        return {
            "proposals": [
                {
                    "id": "p1",
                    "status": "open",
                    "category": "development",
                    "tags": [],
                    "title": "Add dark mode",
                    "score": 3,
                    "comment_count": 2,
                    "author_handle": "alice",
                }
            ]
        }

    async def create_proposal(self, title, body, category="development", tags=None):
        self.calls.append(("create_proposal", title, body, category, tags))
        return {"id": "p2"}

    async def accept_answer(self, post_id, comment_id):
        self.calls.append(("accept_answer", post_id, comment_id))
        return {"accepted_comment_id": comment_id}

    async def get_rules(self):
        self.calls.append(("get_rules",))
        return {"version": 2}

    async def ack_rules(self, version):
        self.calls.append(("ack_rules", version))
        return {"ok": True, "acked_version": version}

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
        "community_post",
        "community_post_proposal",
        "community_comment",
        "community_vote",
        "community_accept_answer",
        "community_draft_patch",
        "community_open_pr",
        "community_submit_patch",
        "community_review_pr",
        "community_start_contribution",
        "community_review_code",
        "community_list_work_orders",
        "community_claim_work_order",
        "community_work_order_lease",
    }


async def test_read_feed_formats_proposals():
    client = FakeClient()
    tool = community_tools.CommunityReadFeedTool(client, AutonomyGate("hitl"))
    out = await tool.execute(limit=5)
    assert "Add dark mode" in out
    assert "p1" in out
    assert client.calls[0] == ("read_feed", 5, None, None, None)


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


async def test_comment_caches_rules_delivered_on_onboarding(monkeypatch):
    # The welcome reply completes onboarding; the server returns the accepted
    # rules and the comment tool caches them on the community config (#33).
    class OnboardingClient(FakeClient):
        async def post_comment(self, thread_id, content, reply_to=None):
            self.calls.append(("post_comment", thread_id, content, reply_to))
            return {
                "comment_id": "c1",
                "rules": {"version": 5, "items": [{"title": "Be kind", "text": "always"}]},
            }

    synced: list = []

    async def fake_sync(community_config, *, rules=None, **kw):
        synced.append((community_config, rules))
        return True

    monkeypatch.setattr(
        "mira_engine.community.rules.sync_community_rules", fake_sync
    )

    client = OnboardingClient()
    cfg = _cfg()
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("fully_autonomous"), cfg)
    out = await tool.execute(thread_id="ob", content="hello")
    assert "Comment posted" in out
    assert len(synced) == 1
    assert synced[0][1] == {"version": 5, "items": [{"title": "Be kind", "text": "always"}]}


async def test_comment_without_delivered_rules_does_not_sync(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("no sync when the response carries no rules")

    monkeypatch.setattr("mira_engine.community.rules.sync_community_rules", boom)
    client = FakeClient()
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("fully_autonomous"), _cfg())
    out = await tool.execute(thread_id="p1", content="nice")
    assert "Comment posted" in out


async def test_proposal_high_impact_queued_in_hybrid(monkeypatch):
    client = FakeClient()
    gate = AutonomyGate("hybrid")
    monkeypatch.setattr(gate, "enqueue_approval", lambda a, p: "appr-2")
    tool = community_tools.CommunityPostProposalTool(client, gate)
    out = await tool.execute(title="T", body="B")
    assert "approval" in out.lower()
    assert client.calls == []


async def test_community_post_discussion_low_impact_runs_in_hybrid():
    client = FakeClient()
    tool = community_tools.CommunityPostTool(client, AutonomyGate("hybrid"))
    out = await tool.execute(title="T", body="B", category="discussion", tags=["ml"])
    assert "discussion" in out.lower()
    assert ("create_proposal", "T", "B", "discussion", ["ml"]) in client.calls


async def test_community_post_development_high_impact_queued_in_hybrid(monkeypatch):
    client = FakeClient()
    gate = AutonomyGate("hybrid")
    captured: dict = {}
    monkeypatch.setattr(
        gate,
        "enqueue_approval",
        lambda a, p: captured.update(action=a, payload=p) or "appr-d",
    )
    tool = community_tools.CommunityPostTool(client, gate)
    out = await tool.execute(title="T", body="B", category="development")
    assert "approval" in out.lower()
    assert captured["action"] == "post_proposal"
    assert client.calls == []


async def test_accept_answer_runs_when_autonomous():
    client = FakeClient()
    tool = community_tools.CommunityAcceptAnswerTool(client, AutonomyGate("fully_autonomous"))
    out = await tool.execute(post_id="q1", comment_id="c9")
    assert "accepted" in out.lower()
    assert ("accept_answer", "q1", "c9") in client.calls


async def test_comment_auto_acks_rules_then_retries(monkeypatch):
    from mira_engine.community.client import CommunityRuleError

    class BlockingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self._blocked = True

        async def post_comment(self, thread_id, content, reply_to=None):
            # Block once with a rules-ack denial, succeed after the ack.
            if self._blocked:
                self._blocked = False
                raise CommunityRuleError(
                    "/agents/messages blocked: rules acknowledgement required",
                    rule_id="accept-rules",
                    reason="must ack v2",
                    how_to_resolve="POST /agents/rules/ack",
                    status_code=403,
                )
            return await super().post_comment(thread_id, content, reply_to)

    client = BlockingClient()
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("fully_autonomous"))
    out = await tool.execute(thread_id="p1", content="hi")
    assert "Comment posted" in out
    # It signed the current rules version and retried the comment.
    assert ("get_rules",) in client.calls
    assert ("ack_rules", 2) in client.calls
    assert ("post_comment", "p1", "hi", None) in client.calls


async def test_non_rules_denial_is_not_auto_acked():
    from mira_engine.community.client import CommunityRuleError

    class OnboardingBlockedClient(FakeClient):
        async def post_comment(self, thread_id, content, reply_to=None):
            raise CommunityRuleError(
                "/agents/messages blocked: onboarding required",
                rule_id="onboarding",
                reason="complete the connection test",
                how_to_resolve="reply in the onboarding thread",
                status_code=403,
            )

    client = OnboardingBlockedClient()
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("fully_autonomous"))
    try:
        await tool.execute(thread_id="p1", content="hi")
        raise AssertionError("expected CommunityRuleError to propagate")
    except CommunityRuleError as e:
        assert e.rule_id == "onboarding"
    # No ack attempt for a non-rules denial.
    assert ("ack_rules", 2) not in client.calls


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
    out = await tool.execute(proposal_id="p1", repo="o/r", diff="diff --git a/x b/x\n", title="Fix")
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


class _ContribClient:
    def __init__(self):
        self.calls: list[tuple] = []

    async def get_contribution(self):
        return {"landing_branch": "contrib", "lanes": {"plugin": {"paths": ["plugins/**"]}}}

    async def review_pr(self, pr_id, verdict, head_sha, body=""):
        self.calls.append(("review_pr", pr_id, verdict, head_sha, body))
        return {"review_score": 2}

    async def get_external_pr(self, pr_id):
        return {
            "pr": {
                "repo": "o/r", "pr_number": 7, "url": "https://github.com/o/r/pull/7", "state": "open",
                "ci_state": "passed", "lane": "plugin", "base_ref": "contrib", "head_sha": "abc123def4567",
                "policy": {"violations": ["too big"], "base_reason": None},
            },
            "reviews": [{"reviewer_name": "bob", "verdict": "approve", "current": True}],
        }


async def test_start_contribution_uses_landing_branch_and_user_lanes(monkeypatch, tmp_path):
    from mira_engine.community import contribute

    seen = {}

    async def fake_start(**kw):
        seen.update(kw)
        scope = contribute.ContributionScope(repo=kw["repo"], landing_branch=kw["landing_branch"], branch="mira/x-1")
        return tmp_path / "ws", scope

    monkeypatch.setattr(contribute, "start_contribution", fake_start)
    cfg = SimpleNamespace(contribution_lanes=["plugin"])
    tool = community_tools.CommunityStartContributionTool(_ContribClient(), AutonomyGate("hitl"), cfg, tmp_path)
    out = await tool.execute(repo="o/r", title="x")
    assert seen["landing_branch"] == "contrib"
    assert seen["allowed_lanes"] == ["plugin"]
    assert seen["plugin_lane_paths"] == ["plugins/**"]
    assert seen["workspace"] == tmp_path
    assert "community_open_pr" in out and "contrib" in out


async def test_review_code_records_head_bound_review():
    client = _ContribClient()
    tool = community_tools.CommunityReviewCodeTool(client, AutonomyGate("fully_autonomous"))
    out = await tool.execute(pr_id="pr1", verdict="approve", head_sha="abc123def4567", body="read it")
    assert client.calls == [("review_pr", "pr1", "approve", "abc123def4567", "read it")]
    assert "score now 2" in out


async def test_review_code_is_low_impact_in_hybrid():
    client = _ContribClient()
    tool = community_tools.CommunityReviewCodeTool(client, AutonomyGate("hybrid"))
    await tool.execute(pr_id="pr1", verdict="request_changes", head_sha="h", body="b")
    assert client.calls  # ran directly, not queued


async def test_review_pr_by_id_shows_head_lane_and_findings():
    tool = community_tools.CommunityReviewPrTool(_ContribClient(), AutonomyGate("hitl"))
    out = await tool.execute(pr_id="pr1")
    assert "head_sha=abc123def4567" in out and "lane=plugin" in out
    assert "too big" in out and "bob=approve" in out


class _WorkOrderClient(_ContribClient):
    def __init__(self, wo=None, claims=None):
        super().__init__()
        self.wo = wo or {
            "id": "wo1", "title": "Edit events", "kind": "plugin_feature", "lane": "plugin",
            "repo": "o/r", "allowed_paths": ["plugins/telegram/**"], "reward": 5,
        }
        self.claims = claims or []

    async def get_work_order(self, wid):
        return {"work_order": self.wo}

    async def list_work_orders(self):
        return {"claimable": [self.wo], "claims": self.claims}

    async def claim_work_order(self, wid):
        self.calls.append(("claim", wid))
        return {"claim": {"expires_at": "2026-10-22T00:00:00Z"}}

    async def heartbeat_work_order(self, wid, progress=""):
        self.calls.append(("heartbeat", wid, progress))
        return {"claim": {"expires_at": "later"}}

    async def release_work_order(self, wid):
        self.calls.append(("release", wid))
        return {}


async def test_claim_respects_the_users_kinds_and_lanes():
    client = _WorkOrderClient()
    cfg = SimpleNamespace(work_order_kinds=["docs"], contribution_lanes=["plugin", "core"], max_active_work_orders=1)
    tool = community_tools.CommunityClaimWorkOrderTool(client, AutonomyGate("fully_autonomous"), cfg)
    out = await tool.execute(work_order_id="wo1")
    assert out.startswith("Not claiming") and client.calls == []
    cfg = SimpleNamespace(work_order_kinds=[], contribution_lanes=["core"], max_active_work_orders=1)
    tool = community_tools.CommunityClaimWorkOrderTool(client, AutonomyGate("fully_autonomous"), cfg)
    assert (await tool.execute(work_order_id="wo1")).startswith("Not claiming")


async def test_claim_respects_the_active_limit():
    client = _WorkOrderClient(claims=[{"status": "active", "work_order": {}}])
    cfg = SimpleNamespace(work_order_kinds=[], contribution_lanes=["plugin"], max_active_work_orders=1)
    tool = community_tools.CommunityClaimWorkOrderTool(client, AutonomyGate("fully_autonomous"), cfg)
    out = await tool.execute(work_order_id="wo1")
    assert "1 active work order" in out and client.calls == []


async def test_claim_is_high_impact_in_hybrid(monkeypatch):
    queued = []
    gate = AutonomyGate("hybrid")
    monkeypatch.setattr(gate, "enqueue_approval", lambda action, payload: queued.append(action) or "a1")
    client = _WorkOrderClient()
    cfg = SimpleNamespace(work_order_kinds=[], contribution_lanes=["plugin"], max_active_work_orders=1)
    out = await community_tools.CommunityClaimWorkOrderTool(client, gate, cfg).execute(work_order_id="wo1")
    assert queued == ["claim_work_order"] and "approval" in out and client.calls == []


async def test_claim_then_start_contribution_takes_scope_from_the_order(monkeypatch, tmp_path):
    from mira_engine.community import contribute

    client = _WorkOrderClient()
    cfg = SimpleNamespace(work_order_kinds=[], contribution_lanes=["plugin"], max_active_work_orders=1)
    out = await community_tools.CommunityClaimWorkOrderTool(client, AutonomyGate("fully_autonomous"), cfg).execute(
        work_order_id="wo1"
    )
    assert ("claim", "wo1") in client.calls and "community_start_contribution" in out

    seen = {}

    async def fake_start(**kw):
        seen.update(kw)
        return tmp_path, contribute.ContributionScope(repo=kw["repo"], landing_branch="contrib", branch="mira/x")

    monkeypatch.setattr(contribute, "start_contribution", fake_start)
    tool = community_tools.CommunityStartContributionTool(client, AutonomyGate("hitl"), cfg, tmp_path)
    await tool.execute(work_order_id="wo1")
    assert seen["repo"] == "o/r" and seen["allowed_paths"] == ["plugins/telegram/**"]
    assert seen["work_order_id"] == "wo1"


async def test_lease_renew_and_release_are_low_impact():
    client = _WorkOrderClient()
    tool = community_tools.CommunityWorkOrderLeaseTool(client, AutonomyGate("hybrid"))
    assert "renewed" in await tool.execute(work_order_id="wo1", action="renew", progress="half")
    assert "released" in await tool.execute(work_order_id="wo1", action="release")
    assert client.calls == [("heartbeat", "wo1", "half"), ("release", "wo1")]


async def test_list_marks_orders_the_user_does_not_allow():
    client = _WorkOrderClient()
    cfg = SimpleNamespace(work_order_kinds=["docs"], contribution_lanes=["plugin"])
    out = await community_tools.CommunityListWorkOrdersTool(client, AutonomyGate("hitl"), cfg).execute()
    assert "wo1" in out and "not allowed" in out


async def test_open_pr_registers_the_workspace_work_order(monkeypatch, tmp_path):
    import json as _json

    from mira_engine.community import contribute

    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    contribute.scope_path(ws).write_text(
        _json.dumps({"repo": "o/r", "landing_branch": "contrib", "branch": "mira/x", "work_order_id": "wo1"})
    )

    async def fake_open(**kw):
        return contribute.OpenPrResult(url="https://github.com/o/r/pull/9", number=9, branch="mira/x", login="alice")

    monkeypatch.setattr(contribute, "open_pr_github", fake_open)
    registered = {}

    class _C:
        async def register_pr(self, *args, **kw):
            registered.update(kw, args=args)
            return {"pr": {"id": "p9", "state": "open"}}

    tool = community_tools.CommunityOpenPrTool(_C(), AutonomyGate("fully_autonomous"))
    out = await tool.execute(working_dir=str(ws), title="t")
    assert registered["work_order_id"] == "wo1" and registered["args"][1] == "o/r"
    assert "p9" in out
