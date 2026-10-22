"""News about the agent's own PRs and proposals goes back to the chat it came from (#72)."""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.channels.community import CommunityChannel
from mira_engine.community import origins


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish_inbound(self, msg) -> None:
        self.published.append(msg)


def _channel() -> tuple[CommunityChannel, _FakeBus]:
    bus = _FakeBus()
    config = SimpleNamespace(api_base="http://x/community", agent_token="tok", agent_id="a1", rules_version=0, rules_text="")
    return CommunityChannel(config, bus), bus


async def test_a_merge_of_the_agents_pr_is_reported_in_its_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(origins, "get_data_dir", lambda: tmp_path)
    origins.remember("pr", "pr-1", {"channel": "ui", "chat_id": "c7", "project_id": "P"}, url="https://x/pull/1", plugin_ids=["demo"], working_dir="/ws")
    channel, bus = _channel()
    await channel._handle_event({"type": "pr_merged", "pr_id": "pr-1"})
    system = [m for m in bus.published if m.channel == "system"]
    assert len(system) == 1
    assert system[0].chat_id == "ui:c7" and system[0].metadata == {"project_id": "P"}
    assert "was merged" in system[0].content and "mira plugin enable" in system[0].content
    # The event still reaches the agent as a community turn, as before.
    assert any(m.channel == "community" for m in bus.published)


async def test_events_about_other_peoples_work_are_not_relayed(tmp_path, monkeypatch):
    monkeypatch.setattr(origins, "get_data_dir", lambda: tmp_path)
    channel, bus = _channel()
    await channel._handle_event({"type": "pr_merged", "pr_id": "someone-elses"})
    assert all(m.channel != "system" for m in bus.published)
