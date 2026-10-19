"""Community budget/triage (#213), persona (#212) and harvest report (#214)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mira_engine.community import approvals
from mira_engine.community.budget import CommunityBudget, event_priority, should_handle
from mira_engine.community.harvest import build_report, interests, is_due, report_path, write_report
from mira_engine.community.persona import apply_persona, draft_persona, persona_path, propose_persona_update
from mira_engine.community.privacy import PrivacyGuard

USER_MD = """# User Profile

## Basic Information

- **Name**: Dana Quill
- **Preferred language(s)**: Chinese, English

## Communication Style

- Concise and practical

## Topics of Interest

- Retrieval-augmented generation for PDFs
- Medical imaging segmentation
- Project Bluefin internals

## Special Instructions

- My employer is Initech; never mention it
"""


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("mira_engine.config.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("mira_engine.community.approvals.get_data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()


# --------------------------------------------------------------------------- #
# #213 budget + triage                                                         #
# --------------------------------------------------------------------------- #


def test_priorities():
    assert event_priority({"type": "mention"}) == "high"
    assert event_priority({"type": "work_order_lease"}) == "high"
    assert event_priority({"type": "needs_review"}) == "medium"
    assert event_priority({"type": "active_discussion"}) == "low"
    assert event_priority(None) == "low"


def test_lower_priorities_pause_as_the_budget_runs_down(tmp_path):
    b = CommunityBudget(1000, tmp_path / "b.json")
    assert all(b.admits(p) for p in ("high", "medium", "low"))
    b.record_turn(600)
    assert b.admits("medium") and not b.admits("low")
    b.record_turn(250)
    assert b.admits("high") and not b.admits("medium")
    b.record_turn(200)
    assert not b.admits("high")
    assert b.today().turns == 3 and b.today().tokens == 1050


def test_zero_limit_is_unlimited(tmp_path):
    b = CommunityBudget(0, tmp_path / "b.json")
    b.record_turn(10**9)
    assert b.admits("low")


async def test_triage_only_runs_for_low_priority_and_costs_budget(tmp_path):
    b = CommunityBudget(10_000, tmp_path / "b.json")
    calls = []

    async def triage(content):
        calls.append(content)
        return False, 40

    ok, why = await should_handle({"type": "mention"}, "hi @me", b, triage=triage)
    assert ok and calls == []
    ok, why = await should_handle({"type": "active_discussion"}, "chatter", b, triage=triage)
    assert not ok and "triage" in why and calls == ["chatter"]
    day = b.today()
    assert day.triage_tokens == 40 and day.tokens == 40 and day.skipped == 1


async def test_exhausted_budget_skips_without_triage(tmp_path):
    b = CommunityBudget(100, tmp_path / "b.json")
    b.record_turn(100)

    async def triage(content):
        raise AssertionError("should not be called")

    ok, why = await should_handle({"type": "mention"}, "x", b, triage=triage)
    assert not ok and "budget" in why


async def test_agent_loop_skips_events_the_budget_does_not_admit(tmp_path, monkeypatch):
    from mira_engine.bus.events import InboundMessage
    from tests.test_agent_loop_core import _make_real_loop

    loop = _make_real_loop(tmp_path)
    loop.community_config = SimpleNamespace(daily_token_budget=100, triage_model="", domains=[])
    ran = []

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        ran.append(1)
        loop._last_loop_tokens_used = 90
        return "done", [], messages + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)
    thread = "24110f35-3c7d-4006-81e4-02a54584a7a4"
    low = InboundMessage(channel="community", sender_id="c", chat_id=thread, content="chat", metadata={"community_event": {"type": "active_discussion"}})
    assert await loop._process_message(low) is not None and ran == [1]  # 100% left: admitted
    assert await loop._process_message(low) is None and ran == [1]  # 10% left: low paused
    high = InboundMessage(channel="community", sender_id="c", chat_id=thread, content="@me", metadata={"community_event": {"type": "mention"}})
    assert await loop._process_message(high) is not None and ran == [1, 1]


# --------------------------------------------------------------------------- #
# #212 persona                                                                 #
# --------------------------------------------------------------------------- #


def _guard():
    return PrivacyGuard(["Dana Quill", "Bluefin", "Initech"])


def test_persona_keeps_interests_and_drops_identity():
    draft = draft_persona([USER_MD], {"domains": ["rag"], "tech_stack": ["python"], "bio": "Answers RAG questions."}, _guard())
    assert "Retrieval-augmented generation for PDFs" in draft and "Medical imaging segmentation" in draft
    assert "Concise and practical" in draft and "Chinese, English" in draft
    assert "**Domains**: rag" in draft and "Answers RAG questions." in draft
    for private in ("Dana", "Bluefin", "Initech", "employer", "Special Instructions"):
        assert private not in draft


def test_persona_changes_wait_for_the_users_approval(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "USER.md").write_text(USER_MD)
    approval_id = propose_persona_update(ws, None, _guard())
    assert approval_id and not persona_path(ws).exists()
    assert propose_persona_update(ws, None, _guard()) is None  # same draft already pending
    rec = approvals.get_approval(approval_id)
    assert rec["action"] == "persona_update" and "+- Medical imaging segmentation" in rec["payload"]["diff"]


async def test_approving_the_persona_writes_it_locally(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "USER.md").write_text(USER_MD)
    rec = approvals.get_approval(propose_persona_update(ws, None, _guard()))
    result = await approvals.execute_approval(rec, SimpleNamespace(api_base="", agent_token=""))
    assert result["ok"] and "Medical imaging" in persona_path(ws).read_text()
    assert propose_persona_update(ws, None, _guard()) is None  # up to date now


# --------------------------------------------------------------------------- #
# #214 harvest report                                                          #
# --------------------------------------------------------------------------- #


class _ReadOnlyClient:
    def __init__(self):
        self.posted = []

    async def _post(self, *a, **k):  # pragma: no cover - must never be called
        self.posted.append(a)
        raise AssertionError("the report must not write to the community")

    async def get_me(self):
        return {"level_label": "Lv3", "level_name": "Regular", "reputation": 31, "badges": [{"badge_id": "first-merge"}]}

    async def list_prs(self):
        today = date.today().isoformat()
        return {"prs": [{"repo": "o/r", "pr_number": 7, "state": "merged", "url": "u", "created_at": today + "T00:00:00Z"}]}

    async def list_work_orders(self):
        return {"claims": [{"status": "active", "expires_at": "soon", "work_order": {"title": "Edit events"}}]}

    async def get_tasks(self):
        return {"tasks": [{"type": "mention", "title": "You were mentioned on X"}, {"type": "needs_vote", "title": "vote"}]}

    async def read_feed(self, limit=20, **kw):
        return {"proposals": [
            {"id": "p1", "title": "Chunking PDFs for retrieval", "tags": ["rag"], "category": "question", "score": 3, "comment_count": 4},
            {"id": "p2", "title": "Kubernetes autoscaling", "tags": [], "category": "discussion"},
        ]}


async def test_report_is_local_read_only_and_tailored(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "USER.md").write_text(USER_MD)
    client = _ReadOnlyClient()
    budget = CommunityBudget(300_000, tmp_path / "b.json")
    budget.record_turn(1234)
    budget.record_skip()
    path = await write_report(client, ws, SimpleNamespace(domains=["rag"]), budget)
    text = path.read_text()
    assert client.posted == []
    assert path == report_path(ws)
    assert "Lv3" in text and "first-merge" in text and "o/r#7: **merged**" in text and "Edit events" in text
    assert "Chunking PDFs for retrieval" in text and "Kubernetes" not in text
    assert "You were mentioned on X" in text and "vote" not in text.split("## Waiting")[1].split("##")[0]
    assert "1 community turns, 1,234 tokens" in text and "1 events skipped" in text


def test_interests_come_from_topics_and_domains(tmp_path):
    (tmp_path / "USER.md").write_text(USER_MD)
    words = interests(tmp_path, ["rag"])
    assert {"rag", "retrieval-augmented", "medical", "segmentation"} <= words
    assert "dana" not in words  # Basic Information is not an interest


def test_report_cadence(tmp_path):
    assert is_due(tmp_path, 7)
    p = report_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("x")
    assert not is_due(tmp_path, 7)
    assert is_due(tmp_path, 7, today=date.today() + timedelta(days=7))
    assert not is_due(tmp_path, 0)
