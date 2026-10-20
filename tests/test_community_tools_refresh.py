"""Joining the community while the gateway runs gives the agent its community tools."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from mira_engine.agent.loop import AgentLoop
from mira_engine.agent.tools.community import live_community_login_key
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import CommunityConfig


def _save(path: Path, **community) -> None:
    path.write_text(json.dumps({"community": community}), encoding="utf-8")


def _loop(tmp_path: Path, monkeypatch, community: CommunityConfig | None) -> AgentLoop:
    monkeypatch.setattr("mira_engine.config.loader.get_config_path", lambda: tmp_path / "config.json")
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model", community_config=community)


def _community_tools(loop: AgentLoop) -> list[str]:
    return [n for n in loop.tools.all_tool_names if n.startswith("community_")]


def test_logging_in_while_running_adds_the_tools_and_logging_out_removes_them(tmp_path, monkeypatch):
    _save(tmp_path / "config.json", apiBase="http://c.test")
    loop = _loop(tmp_path, monkeypatch, CommunityConfig(api_base="http://c.test"))
    loop._set_tool_context("ui", "chat-1")
    assert _community_tools(loop) == []

    # The desktop app's join flow saves the token.
    _save(tmp_path / "config.json", apiBase="http://c.test", agentToken="tok", agentName="RYU")
    loop._set_tool_context("ui", "chat-1")
    assert "community_post" in _community_tools(loop) and "community_claim_proposal" in _community_tools(loop)
    assert loop.community_config.agent_name == "RYU"

    # Unchanged login: the same tool objects stay.
    post = loop.tools.get("community_post")
    loop._set_tool_context("ui", "chat-1")
    assert loop.tools.get("community_post") is post

    _save(tmp_path / "config.json", apiBase="http://c.test")
    loop._set_tool_context("ui", "chat-1")
    assert _community_tools(loop) == []


def test_loops_without_a_community_config_never_read_the_file(tmp_path, monkeypatch):
    _save(tmp_path / "config.json", apiBase="http://c.test", agentToken="tok")
    loop = _loop(tmp_path, monkeypatch, None)
    loop._set_tool_context("ui", "chat-1")
    assert _community_tools(loop) == []


def test_the_live_key_reads_both_spellings_and_tolerates_a_bad_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr("mira_engine.config.loader.get_config_path", lambda: path)
    assert live_community_login_key() is None
    path.write_text("{oops")
    assert live_community_login_key() is None
    _save(path, api_base=" http://c.test ", agent_token="t", enabled=False)
    assert live_community_login_key() == (False, "http://c.test", "t")
