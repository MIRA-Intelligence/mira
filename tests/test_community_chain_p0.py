"""The contribution chain from a chat to a merged change (mira-community#72, P0–P2):
held PRs can be approved and opened, approvals are decided from any chat or the
CLI, follow-up pushes update the PR, and news about the agent's own proposals
and PRs comes back to the conversation they started in."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mira_engine.agent.tools import community as community_tools
from mira_engine.bus.events import InboundMessage
from mira_engine.command.builtin import cmd_approvals, cmd_approve, cmd_reject
from mira_engine.command.router import CommandContext
from mira_engine.community import approvals, contribute, origins
from mira_engine.community.autonomy import AutonomyGate
from mira_engine.community.proposals import community_discovery_hint

BRANCH = "mira/fix-abc123"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(origins, "get_data_dir", lambda: tmp_path)
    return tmp_path


class FakeRunner:
    def __init__(self, responses=None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    async def __call__(self, cmd, cwd):
        self.calls.append(cmd)
        key = " ".join(cmd)
        for pat, result in self.responses.items():
            if pat in key:
                return result
        return (0, "", "")

    def ran(self, substr: str) -> bool:
        return any(substr in " ".join(c) for c in self.calls)


def _workspace(tmp_path, **scope_kw):
    ws = tmp_path / "contrib"
    (ws / ".git").mkdir(parents=True)
    scope = {"repo": "o/r", "landing_branch": "contrib", "branch": BRANCH, **scope_kw}
    contribute.scope_path(ws).write_text(json.dumps(scope))
    return str(ws)


def _runner(**extra):
    return FakeRunner({
        "rev-parse --abbrev-ref HEAD": (0, BRANCH + "\n", ""),
        "status --porcelain": (0, "?? plugins/x/a.py\0", ""),
        "diff --name-only": (0, "plugins/x/a.py\0docs/b.md\0", ""),
        "git diff origin/": (0, "+print('hi')\n", ""),
        "gh api user": (0, "alice\n", ""),
        "gh pr create": (0, "https://github.com/o/r/pull/42\n", ""),
        **extra,
    })


class FakeClient:
    def __init__(self, fail=False):
        self.registered: list[dict] = []
        self.fail = fail

    async def register_pr(self, proposal_id, repo, pr_number, url, author_login, **kw):
        if self.fail:
            raise RuntimeError("platform down")
        self.registered.append({"proposal_id": proposal_id, "repo": repo, "pr_number": pr_number, **kw})
        return {"pr": {"id": "pr-1", "state": "open"}}


# --------------------------------------------------------------------------- #
# contribute: follow-up pushes, gh precheck, the shared open-and-register path  #
# --------------------------------------------------------------------------- #


async def test_a_second_open_pr_pushes_to_the_existing_pr_instead_of_creating_one(tmp_path):
    runner = _runner(**{"gh pr list": (0, json.dumps([{"number": 42, "url": "https://github.com/o/r/pull/42"}]), "")})
    result = await contribute.open_pr_github(working_dir=_workspace(tmp_path), title="follow-up", runner=runner)
    assert result.updated is True and result.number == 42
    assert runner.ran("git push") and not runner.ran("gh pr create")
    assert result.changed_paths == ["plugins/x/a.py", "docs/b.md"]


async def test_open_pr_creates_when_gh_pr_list_is_empty_or_unavailable(tmp_path):
    for listing in [(0, "[]", ""), (1, "", "unknown flag")]:
        runner = _runner(**{"gh pr list": listing})
        result = await contribute.open_pr_github(working_dir=_workspace(tmp_path / str(listing[0])), title="t", runner=runner)
        assert result.updated is False and runner.ran("gh pr create")


async def test_start_contribution_checks_gh_login_before_cloning(tmp_path):
    runner = FakeRunner({"gh auth status": (1, "", "You are not logged into any GitHub hosts")})
    with pytest.raises(contribute.ContributeError, match="gh auth login"):
        await contribute.start_contribution(repo="o/r", title="t", landing_branch="contrib", workspace=tmp_path, runner=runner)
    assert not runner.ran("git clone")


def test_plugin_ids_from_paths():
    assert contribute.plugin_ids_from_paths(["plugins/tg/a.py", "plugins/tg/b/c.py", "plugins/x/plugin.json", "docs/a.md", "plugins/"]) == ["tg", "x"]


async def test_open_and_register_pr_registers_and_reports(tmp_path):
    client = FakeClient()
    out = await contribute.open_and_register_pr(
        client, working_dir=_workspace(tmp_path, proposal_id="prop-1", work_order_id="wo-1"), title="t", runner=_runner()
    )
    assert out["registered"] and out["pr_id"] == "pr-1" and out["plugin_ids"] == ["x"]
    assert client.registered[0]["proposal_id"] == "prop-1" and client.registered[0]["work_order_id"] == "wo-1"
    assert "Opened and registered PR https://github.com/o/r/pull/42 (id pr-1" in contribute.pr_summary(out)
    failed = await contribute.open_and_register_pr(FakeClient(fail=True), working_dir=_workspace(tmp_path / "b"), title="t", runner=_runner())
    assert failed["registered"] is False and "platform down" in contribute.pr_summary(failed)
    updated = await contribute.open_and_register_pr(
        FakeClient(), working_dir=_workspace(tmp_path / "c"), title="t",
        runner=_runner(**{"gh pr list": (0, json.dumps([{"number": 42, "url": "u"}]), "")}),
    )
    assert updated["updated"] and "follow-up commits" in contribute.pr_summary(updated)


# --------------------------------------------------------------------------- #
# approvals: the open_pr branch, expiry, reminders, follow-ups                 #
# --------------------------------------------------------------------------- #


async def test_an_approved_open_pr_is_opened_registered_and_remembered(monkeypatch, data_dir):
    seen = {}

    async def fake_open(client, **kw):
        seen.update(kw)
        return {"url": "https://github.com/o/r/pull/7", "number": 7, "updated": False, "registered": True,
                "pr_id": "pr-7", "state": "open", "plugin_ids": ["demo"], "error": ""}

    monkeypatch.setattr(contribute, "open_and_register_pr", fake_open)
    monkeypatch.setattr("mira_engine.community.client.CommunityClient", lambda base, token: object())
    rec = {
        "id": "a1", "action": "open_pr",
        "payload": {"working_dir": "/ws", "title": "Add retry", "body": "b", "proposal_id": "p1"},
        "origin": {"channel": "ui", "chat_id": "chat-9"},
    }
    res = await approvals.execute_approval(rec, SimpleNamespace(api_base="http://x", agent_token="t"))
    assert res["ok"] and res["pr_url"].endswith("/pull/7") and "pr-7" in res["detail"]
    assert seen["working_dir"] == "/ws" and seen["proposal_id"] == "p1"
    remembered = origins.lookup("pr", "pr-7")
    assert remembered["origin"] == {"channel": "ui", "chat_id": "chat-9"} and remembered["plugin_ids"] == ["demo"]


async def test_an_open_pr_that_cannot_be_opened_fails_cleanly(monkeypatch, data_dir):
    async def boom(client, **kw):
        raise contribute.ContributeError("refusing to push: the change contains a credential")

    monkeypatch.setattr(contribute, "open_and_register_pr", boom)
    monkeypatch.setattr("mira_engine.community.client.CommunityClient", lambda base, token: object())
    rec = {"id": "a2", "action": "open_pr", "payload": {"working_dir": "/ws", "title": "t"}}
    res = await approvals.execute_approval(rec, SimpleNamespace(api_base="http://x", agent_token="t"))
    assert res["ok"] is False and "credential" in res["detail"]


def test_pending_approvals_expire_after_the_ttl(data_dir):
    fresh = approvals.append_approval("comment", {"content": "hi"})
    old = approvals.append_approval("post_proposal", {"title": "Old idea"})
    records = approvals._latest()
    records[old]["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=approvals.APPROVAL_TTL_HOURS + 1)).isoformat()
    approvals._write_all(list(records.values()))
    pending = approvals.list_approvals(status="pending")
    assert [r["id"] for r in pending] == [fresh]
    assert approvals.get_approval(old)["status"] == "expired"


def test_reminders_are_due_once_a_day_for_old_pending_items(data_dir):
    rid = approvals.append_approval("claim_proposal", {"proposal_id": "p1", "title": "Dark mode"})
    assert approvals.due_for_reminder() == []  # too fresh
    records = approvals._latest()
    records[rid]["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=approvals.REMIND_AFTER_HOURS + 1)).isoformat()
    approvals._write_all(list(records.values()))
    due = approvals.due_for_reminder()
    assert [r["id"] for r in due] == [rid] and approvals.summarize(due[0]) == "claim_proposal: Dark mode"
    assert approvals.due_for_reminder() == []  # reminded; not again today
    assert approvals.get_approval(rid)["reminded_at"]


def test_summarize_is_one_short_line(data_dir):
    rec = {"action": "open_pr", "payload": {"title": "x" * 100}}
    assert approvals.summarize(rec) == "open_pr: " + "x" * 77 + "…"
    assert approvals.summarize({"action": "vote", "payload": {"proposal_id": "p9"}}) == "vote: p9"


async def test_follow_up_prompts_cover_pr_claim_and_failure():
    cfg = SimpleNamespace(api_base="", agent_token="", web_url="http://web.test")
    pr = await approvals.follow_up_prompt(
        {"action": "open_pr", "payload": {"title": "Add retry"}}, {"ok": True, "detail": "Opened PR u"}, cfg
    )
    assert "approved opening the pull request" in pr and "Opened PR u" in pr
    claim = await approvals.follow_up_prompt(
        {"action": "claim_proposal", "payload": {"title": "T", "proposal_id": "p1"}}, {"ok": True}, cfg
    )
    assert "community_start_contribution" in claim
    posted = await approvals.follow_up_prompt(
        {"action": "post_proposal", "payload": {"title": "T"}}, {"ok": True, "post_id": "p1"}, cfg
    )
    assert "http://web.test/proposals/p1" in posted
    failed = await approvals.follow_up_prompt(
        {"action": "open_pr", "payload": {"title": "Add retry"}}, {"ok": False, "detail": "privacy-filter: 'Acme'"}, cfg
    )
    assert "could not be carried out" in failed and "privacy-filter" in failed
    assert await approvals.follow_up_prompt({"action": "vote", "payload": {}}, {"ok": False, "detail": "x"}, cfg) is None
    assert await approvals.follow_up_prompt({"action": "comment", "payload": {}}, {"ok": True}, cfg) is None


# --------------------------------------------------------------------------- #
# slash commands: /approvals, /approve, /reject                                 #
# --------------------------------------------------------------------------- #


def _ctx(raw: str, args: str = "", loop=None) -> CommandContext:
    msg = InboundMessage(channel="telegram", sender_id="u", chat_id="c1", content=raw)
    return CommandContext(msg=msg, session=None, key="telegram:c1", raw=raw, args=args, loop=loop)


async def test_approvals_command_lists_pending_items(data_dir):
    assert "No community actions" in (await cmd_approvals(_ctx("/approvals"))).content
    rid = approvals.append_approval("post_proposal", {"title": "Dark mode", "body": "b"})
    out = (await cmd_approvals(_ctx("/approvals"))).content
    assert rid in out and "post_proposal: Dark mode" in out and "/approve <id>" in out


async def test_approve_command_runs_the_action_and_follows_up(monkeypatch, data_dir):
    from mira_engine.config.schema import Config

    rid = approvals.append_approval(
        "post_proposal", {"title": "Dark mode", "body": "b", "category": "development"},
        origin={"channel": "ui", "chat_id": "chat-7", "project_id": "PRJ"},
    )

    async def fake_exec(record, community):
        return {"ok": True, "detail": "post created (p1)", "post_id": "p1"}

    monkeypatch.setattr(approvals, "execute_approval", fake_exec)
    monkeypatch.setattr("mira_engine.config.loader.load_config", lambda *a, **k: Config())
    loop = SimpleNamespace(bus=SimpleNamespace(publish_inbound=AsyncMock()))
    out = await cmd_approve(_ctx(f"/approve {rid}", args=f" {rid}", loop=loop))
    assert out.channel == "telegram" and "Approved and done" in out.content
    assert approvals.get_approval(rid)["status"] == "approved"
    follow = loop.bus.publish_inbound.await_args.args[0]
    assert follow.channel == "system" and follow.chat_id == "ui:chat-7" and follow.metadata == {"project_id": "PRJ"}
    assert "Dark mode" in follow.content and "community_claim_proposal" in follow.content
    assert "already approved" in (await cmd_approve(_ctx(f"/approve {rid}", args=f" {rid}", loop=loop))).content


async def test_approve_command_records_a_failure_and_hands_it_back(monkeypatch, data_dir):
    from mira_engine.config.schema import Config

    rid = approvals.append_approval("open_pr", {"title": "Add retry", "working_dir": "/ws"})

    async def fake_exec(record, community):
        return {"ok": False, "detail": "could not open the PR: gh is not logged in"}

    monkeypatch.setattr(approvals, "execute_approval", fake_exec)
    monkeypatch.setattr("mira_engine.config.loader.load_config", lambda *a, **k: Config())
    loop = SimpleNamespace(bus=SimpleNamespace(publish_inbound=AsyncMock()))
    out = await cmd_approve(_ctx(f"/approve {rid}", args=f" {rid}", loop=loop))
    assert "failed" in out.content and approvals.get_approval(rid)["status"] == "failed"
    follow = loop.bus.publish_inbound.await_args.args[0]
    assert follow.chat_id == "telegram:c1" and "could not be carried out" in follow.content


async def test_reject_command_and_bad_ids(data_dir):
    rid = approvals.append_approval("comment", {"content": "hi"})
    assert "Rejected" in (await cmd_reject(_ctx(f"/reject {rid}", args=f" {rid}"))).content
    assert approvals.get_approval(rid)["status"] == "rejected"
    assert "No community approval nope" in (await cmd_approve(_ctx("/approve nope", args=" nope"))).content
    assert "Usage" in (await cmd_approve(_ctx("/approve ", args=" "))).content


# --------------------------------------------------------------------------- #
# origins: news goes back to the chat it came from                              #
# --------------------------------------------------------------------------- #


def test_origins_remember_lookup_and_trim(data_dir):
    assert origins.lookup("pr", "x") is None
    origins.remember("pr", "pr-1", {"channel": "ui", "chat_id": "c"}, url="u", plugin_ids=["demo"])
    origins.remember("pr", "pr-1", None)  # no origin: ignored
    assert origins.lookup("pr", "pr-1")["url"] == "u"
    for i in range(origins.MAX_PER_KIND + 5):
        origins.remember("proposal", f"p{i}", {"channel": "cli", "chat_id": "c"})
    assert origins.lookup("proposal", "p0") is None and origins.lookup("proposal", f"p{origins.MAX_PER_KIND + 4}")
    assert origins.lookup("pr", "pr-1") is not None  # other kinds untouched


def test_origin_follow_up_texts(data_dir):
    origins.remember("pr", "pr-1", {"channel": "ui", "chat_id": "c7", "project_id": "P"}, url="https://x/pull/1", plugin_ids=["demo"], working_dir="/ws")
    origins.remember("proposal", "prop-1", {"channel": "telegram", "chat_id": "t1"}, title="Dark mode")
    text, origin = origins.origin_follow_up({"type": "pr_merged", "pr_id": "pr-1"})
    assert origin == {"channel": "ui", "chat_id": "c7", "project_id": "P"}
    assert "https://x/pull/1" in text and "was merged" in text and "~/.mira/plugins/<id>" in text and "/ws/plugins/<id>" in text
    text, _ = origins.origin_follow_up({"type": "pr_reviewed", "pr_id": "pr-1", "verdict": "request_changes"})
    assert "request_changes" in text and "community_open_pr again" in text
    text, _ = origins.origin_follow_up({"type": "pr_policy", "pr_id": "pr-1", "problems": ["outside the allowed paths: x"]})
    assert "outside the allowed paths" in text
    text, origin = origins.origin_follow_up({"type": "proposal_claimed", "proposal_id": "prop-1", "claimant": "bob"})
    assert origin == {"channel": "telegram", "chat_id": "t1"} and '"Dark mode"' in text and "bob" in text
    text, _ = origins.origin_follow_up({"type": "proposal_rejected", "proposal_id": "prop-1", "score": -3})
    assert "score -3" in text
    assert origins.origin_follow_up({"type": "pr_merged", "pr_id": "unknown"}) is None
    assert origins.origin_follow_up({"type": "reply", "proposal_id": "prop-1"}) is None


# --------------------------------------------------------------------------- #
# tools: held PRs tell the user how to approve; defaults; gate verdict          #
# --------------------------------------------------------------------------- #


async def test_a_held_open_pr_tells_the_user_how_to_approve(monkeypatch, data_dir):
    tool = community_tools.CommunityOpenPrTool(FakeClient(), AutonomyGate("hybrid"))
    tool.set_context("telegram", "c1")
    out = await tool.execute(working_dir="/ws", title="Add retry")
    rid = approvals.list_approvals(status="pending")[0]["id"]
    assert f"/approve {rid}" in out and f"mira community approvals approve {rid}" in out and "/approvals" in out
    assert approvals.get_approval(rid)["origin"] == {"channel": "telegram", "chat_id": "c1"}


async def test_open_pr_tool_remembers_where_it_was_asked(monkeypatch, data_dir, tmp_path):
    async def fake_open(client, **kw):
        return {"url": "https://x/pull/3", "number": 3, "updated": False, "registered": True, "pr_id": "pr-3",
                "state": "open", "plugin_ids": [], "error": ""}

    monkeypatch.setattr(contribute, "open_and_register_pr", fake_open)
    tool = community_tools.CommunityOpenPrTool(FakeClient(), AutonomyGate("fully_autonomous"))
    tool.set_context("ui", "chat-2")
    out = await tool.execute(working_dir=str(tmp_path), title="t")
    assert "pr-3" in out and origins.lookup("pr", "pr-3")["origin"] == {"channel": "ui", "chat_id": "chat-2"}


async def test_start_contribution_defaults_to_the_configured_repo(monkeypatch, tmp_path):
    captured = {}

    async def fake_start(**kw):
        captured.update(kw)
        return tmp_path / "ws", contribute.ContributionScope(repo=kw["repo"], landing_branch="contrib", branch="mira/x")

    monkeypatch.setattr(contribute, "start_contribution", fake_start)

    class _C:
        async def get_contribution(self):
            return {"landing_branch": "contrib", "lanes": {"plugin": {"paths": ["plugins/**"]}}}

    cfg = SimpleNamespace(contribution_lanes=["plugin", "core"], default_repo="MIRA-Intelligence/mira")
    tool = community_tools.CommunityStartContributionTool(_C(), AutonomyGate("fully_autonomous"), cfg, workspace=tmp_path)
    out = await tool.execute(title="Free contribution")
    assert "Contribution workspace ready" in out and captured["repo"] == "MIRA-Intelligence/mira"


async def test_review_pr_shows_the_platforms_gate_verdict():
    class _C:
        async def get_external_pr(self, pr_id):
            return {
                "pr": {"repo": "o/r", "pr_number": 5, "state": "open", "ci_state": "passed", "lane": "core", "head_sha": "abc", "policy": {}},
                "reviews": [],
                "gate": {"checked_sha": "abc", "reasons": ["core-lane change (outside the plugin lane): maintainer approval required"]},
            }

    tool = community_tools.CommunityReviewPrTool(_C(), AutonomyGate("fully_autonomous"))
    out = await tool.execute(pr_id="x")
    assert "merge gate: core-lane change" in out


def test_discovery_hint_depends_on_the_connection():
    cfg = SimpleNamespace(enabled=True)
    assert "community_post_proposal" in community_discovery_hint(cfg, connected=True)
    assert "mira community login" in community_discovery_hint(cfg, connected=False)
    assert community_discovery_hint(SimpleNamespace(enabled=False), connected=False) == ""
