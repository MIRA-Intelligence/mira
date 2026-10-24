"""The loop after the PR (mira-community#72): a red PR comes back to the chat it
was opened from with the next step, follow-up pushes to that PR run without a
new approval (within bounds), and community_pr_status shows the failing checks,
their logs and what to do."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mira_engine.agent.tools import community as community_tools
from mira_engine.channels.community import CommunityChannel
from mira_engine.community import approvals, contribute, origins
from mira_engine.community.autonomy import AutonomyGate
from mira_engine.community.client import CommunityError
from mira_engine.community.digest import _TASK_TOOL_HINT
from mira_engine.community.inbox import kind_of

WS = "/Users/x/.mira/workspace/contributions/mira-fix"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(origins, "get_data_dir", lambda: tmp_path)
    return tmp_path


def _remember_pr(pr_id: str = "pr-1", **extra):
    origins.remember(
        "pr",
        pr_id,
        {"channel": "ui", "chat_id": "c7"},
        url="https://x/pull/1",
        plugin_ids=[],
        working_dir=WS,
        **extra,
    )


# --------------------------------------------------------------------------- #
# origins: the news and the next step reach the chat the PR came from          #
# --------------------------------------------------------------------------- #


def test_ci_and_conflict_news_reach_the_chat_with_the_next_step(data_dir):
    _remember_pr()
    text, origin = origins.origin_follow_up(
        {
            "type": "pr_ci_failed",
            "pr_id": "pr-1",
            "checks": [{"name": "test", "url": "https://ci/1"}, {"name": "lint"}],
        }
    )
    assert origin == {"channel": "ui", "chat_id": "c7"}
    assert "failed CI: test (https://ci/1), lint" in text
    assert "community_pr_status with pr_id pr-1" in text and f"`{WS}`" in text
    assert "community_open_pr again" in text and "unrelated to this change" in text
    text, _ = origins.origin_follow_up(
        {"type": "pr_conflict", "pr_id": "pr-1", "landing_branch": "contrib"}
    )
    assert "no longer merges cleanly" in text and "git merge origin/contrib" in text
    text, _ = origins.origin_follow_up({"type": "pr_ci_passed", "pr_id": "pr-1"})
    assert "passed CI again" in text and "Nothing to do now" in text
    text, _ = origins.origin_follow_up({"type": "pr_closed", "pr_id": "pr-1"})
    assert "closed on the code host" in text and "reopen" in text
    text, _ = origins.origin_follow_up(
        {"type": "pr_reviewed", "pr_id": "pr-1", "verdict": "request_changes"}
    )
    assert "community_review_pr with pr_id pr-1" in text and f"`{WS}`" in text
    assert origins.origin_follow_up({"type": "pr_ci_failed", "pr_id": "unknown"}) is None


def test_the_pr_of_a_workspace_is_found_and_its_fix_rounds_counted(data_dir):
    assert origins.pr_for_working_dir(WS) is None
    _remember_pr("pr-old")
    _remember_pr("pr-1")  # the same workspace, later
    pr_id, entry = origins.pr_for_working_dir(WS)
    assert pr_id == "pr-1" and entry["url"] == "https://x/pull/1"
    origins.update("pr", "pr-1", fix_rounds=2)
    assert origins.pr_for_working_dir(WS)[1]["fix_rounds"] == 2
    origins.update("pr", "missing", fix_rounds=1)  # nothing to update
    assert origins.lookup("pr", "missing") is None
    assert origins.pr_for_working_dir("") is None
    assert origins.pr_for_working_dir("/elsewhere") is None


# --------------------------------------------------------------------------- #
# autonomy: a follow-up push is a lesser decision than a new PR                #
# --------------------------------------------------------------------------- #


class _Client:
    def __init__(self, state: str = "open"):
        self.state = state

    async def get_external_pr(self, pr_id):
        return {"pr": {"id": pr_id, "state": self.state}}


def _fake_open(updated: bool):
    async def fake(client, **kw):
        return {
            "url": "https://x/pull/1",
            "number": 1,
            "updated": updated,
            "registered": True,
            "pr_id": None if updated else "pr-new",
            "state": "open",
            "plugin_ids": [],
            "error": "",
        }

    return fake


def test_update_pr_is_low_impact_only_in_hybrid():
    assert AutonomyGate("hybrid").requires_approval("update_pr") is False
    assert AutonomyGate("hybrid").requires_approval("open_pr") is True
    assert AutonomyGate("hitl").requires_approval("update_pr") is True
    assert AutonomyGate("fully_autonomous").requires_approval("update_pr") is False


async def test_a_follow_up_push_to_an_open_pr_runs_without_a_new_approval(monkeypatch, data_dir):
    _remember_pr()
    monkeypatch.setattr(contribute, "open_and_register_pr", _fake_open(updated=True))
    tool = community_tools.CommunityOpenPrTool(_Client(), AutonomyGate("hybrid"))
    tool.set_context("ui", "c7")
    out = await tool.execute(working_dir=WS, title="Fix the test")
    assert "Pushed the follow-up commits" in out
    assert approvals.list_approvals(status="pending") == []
    assert origins.lookup("pr", "pr-1")["fix_rounds"] == 1


async def test_after_the_round_cap_the_next_push_is_held(monkeypatch, data_dir):
    _remember_pr(fix_rounds=origins.MAX_AUTO_FIX_ROUNDS)
    monkeypatch.setattr(contribute, "open_and_register_pr", _fake_open(updated=True))
    tool = community_tools.CommunityOpenPrTool(_Client(), AutonomyGate("hybrid"))
    tool.set_context("ui", "c7")
    out = await tool.execute(working_dir=WS, title="Fix again")
    assert "Held for your user's approval" in out and "follow-up pushes without a human look" in out
    pending = approvals.list_approvals(status="pending")
    assert (
        len(pending) == 1
        and pending[0]["action"] == "open_pr"
        and pending[0]["payload"]["pr_id"] == "pr-1"
    )


async def test_a_workspace_whose_pr_is_no_longer_open_needs_approval_again(monkeypatch, data_dir):
    _remember_pr()
    monkeypatch.setattr(contribute, "open_and_register_pr", _fake_open(updated=False))
    tool = community_tools.CommunityOpenPrTool(_Client(state="merged"), AutonomyGate("hybrid"))
    tool.set_context("ui", "c7")
    assert "Held for your user's approval" in await tool.execute(working_dir=WS, title="More")
    assert approvals.list_approvals(status="pending")[0]["action"] == "open_pr"


async def test_an_unknown_workspace_still_needs_approval_in_hybrid(data_dir):
    tool = community_tools.CommunityOpenPrTool(_Client(), AutonomyGate("hybrid"))
    tool.set_context("ui", "c7")
    assert "Held for your user's approval" in await tool.execute(
        working_dir="/elsewhere", title="New"
    )


async def test_an_approved_follow_up_push_resets_the_round_count(monkeypatch, data_dir):
    _remember_pr(fix_rounds=3)
    monkeypatch.setattr(contribute, "open_and_register_pr", _fake_open(updated=True))
    cfg = SimpleNamespace(api_base="http://api.test", agent_token="t", web_url="")
    record = {
        "action": "update_pr",
        "payload": {"working_dir": WS, "title": "Fix", "pr_id": "pr-1"},
        "origin": {"channel": "ui", "chat_id": "c7"},
    }
    res = await approvals.execute_approval(record, cfg)
    assert res["ok"] and "Pushed the follow-up commits" in res["detail"]
    assert origins.lookup("pr", "pr-1")["fix_rounds"] == 0
    prompt = await approvals.follow_up_prompt(record, res, cfg)
    assert "approved pushing the follow-up" in prompt and "Pushed the follow-up commits" in prompt


# --------------------------------------------------------------------------- #
# community_pr_status                                                          #
# --------------------------------------------------------------------------- #


class _StatusClient:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error

    async def pr_checks(self, pr_id):
        if self.error:
            raise self.error
        return self.data

    async def list_prs(self):
        return {
            "prs": [
                {
                    "id": "pr-1",
                    "repo": "o/r",
                    "pr_number": 1,
                    "state": "open",
                    "ci_state": "failed",
                    "url": "https://x/pull/1",
                }
            ]
        }


CHECKS = {
    "pr": {
        "id": "pr-1",
        "repo": "o/r",
        "pr_number": 1,
        "url": "https://x/pull/1",
        "state": "open",
        "ci_state": "failed",
        "head_sha": "abc1234def",
        "mergeable": True,
        "lane": "plugin",
        "live": True,
    },
    "gate": {"reasons": ["CI has not passed"], "checked_sha": "abc1234def"},
    "checks": [
        {
            "name": "test",
            "url": "https://ci/1",
            "summary": "1 failed",
            "log_excerpt": "FAIL src/a.test.tsx\nError: expected 1 to be 2",
        }
    ],
    "reasons": ["CI failed: test (https://ci/1)"],
    "next_step": "Read the failing checks below, fix the cause, then call community_open_pr again",
}


async def test_pr_status_shows_the_failing_checks_their_logs_and_the_next_step(data_dir):
    tool = community_tools.CommunityPrStatusTool(_StatusClient(CHECKS), AutonomyGate("hybrid"))
    assert tool.read_only
    out = await tool.execute(pr_id="pr-1")
    assert "PR o/r#1 https://x/pull/1" in out and "state=open ci=failed head=abc1234def" in out
    assert "needs you because: CI failed: test" in out
    assert "merge gate: CI has not passed" in out
    assert "- test — 1 failed (https://ci/1)" in out and "    Error: expected 1 to be 2" in out
    assert out.rstrip().endswith("community_open_pr again")


async def test_pr_status_resolves_the_pr_from_the_workspace_and_lists_without_one(data_dir):
    _remember_pr()
    tool = community_tools.CommunityPrStatusTool(_StatusClient(CHECKS), AutonomyGate("hybrid"))
    assert "PR o/r#1" in await tool.execute(working_dir=WS)
    assert "No pull request was opened from /nowhere" in await tool.execute(working_dir="/nowhere")
    listing = await tool.execute()
    assert (
        "Your registered pull requests" in listing
        and "o/r#1 state=open ci=failed pr_id=pr-1" in listing
    )


async def test_pr_status_explains_someone_elses_pr_and_a_missing_one(data_dir):
    forbidden = community_tools.CommunityPrStatusTool(
        _StatusClient(error=CommunityError("forbidden", status_code=403)), AutonomyGate("hybrid")
    )
    assert "not yours" in await forbidden.execute(pr_id="pr-9")
    missing = community_tools.CommunityPrStatusTool(
        _StatusClient(error=CommunityError("nope", status_code=404)), AutonomyGate("hybrid")
    )
    assert "not registered" in await missing.execute(pr_id="pr-9")


def test_render_pr_status_without_a_code_host_says_so():
    data = {
        "pr": {
            "repo": "o/r",
            "pr_number": 2,
            "state": "open",
            "ci_state": "unknown",
            "live": False,
        },
        "gate": {"reasons": [], "checked_sha": None},
        "checks": [],
        "reasons": [],
        "next_step": "CI is still running; nothing to do yet.",
    }
    out = community_tools.render_pr_status(data)
    assert (
        "as last stored" in out
        and "next step: CI is still running" in out
        and "failing checks" not in out
    )


def test_the_status_tool_is_part_of_the_connected_tool_set():
    cfg = SimpleNamespace(
        enabled=True, api_base="http://api.test", agent_token="t", autonomy_mode="hybrid"
    )
    assert "community_pr_status" in {t.name for t in community_tools.build_community_tools(cfg)}


# --------------------------------------------------------------------------- #
# channel, inbox, heartbeat                                                    #
# --------------------------------------------------------------------------- #


def test_the_community_channel_names_the_checks_and_the_tool():
    text = CommunityChannel._format_event(
        {
            "type": "pr_ci_failed",
            "pr_id": "pr-1",
            "url": "https://x/pull/1",
            "checks": [{"name": "test", "url": "https://ci/1"}, {"name": "lint"}],
        }
    )
    assert "failing checks: test (https://ci/1); lint" in text
    assert "community_pr_status with pr_id pr-1" in text
    assert "community_pr_status" not in CommunityChannel._format_event(
        {"type": "pr_merged", "pr_id": "pr-1"}
    )


def test_new_pr_events_reach_the_inbox_as_work_and_the_heartbeat_knows_the_tool():
    for etype in ("pr_ci_failed", "pr_ci_passed", "pr_conflict", "pr_closed", "pr_needs_attention"):
        assert kind_of({"type": etype}) == "work"
    assert "community_pr_status" in _TASK_TOOL_HINT["pr_needs_attention"]
