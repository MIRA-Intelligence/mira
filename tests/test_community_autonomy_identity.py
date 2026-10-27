"""Autonomy changes apply live; the agent knows its name; welcome-thread rules."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mira_engine.agent.tools import community as community_tools
from mira_engine.community import autonomy
from mira_engine.community.autonomy import AutonomyGate
from mira_engine.community.onboarding import build_onboarding_prompt, onboarding_prompt
from mira_engine.community.rules import WELCOME_THREAD_NOTE, render_rules_text, rules_for_prompt
from mira_engine.profile.onboarding import configured_agent_name, render_soul_local

V6_ITEMS = [
    {"id": "onboarding", "title": "Complete the connection test", "enforcement": "hard",
     "text": "Before participating, reply once in the Welcome / Connection Test thread to confirm your agent is connected."},
    {"id": "accept-rules", "title": "Acknowledge the rules", "enforcement": "hard",
     "text": "Acknowledge the current rules version before writing to the community."},
    {"id": "no-private-data", "title": "Keep private data private", "enforcement": "hard", "text": "Never post secrets."},
]


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr("mira_engine.config.loader.get_config_path", lambda: path)

    def write(mode):
        path.write_text(json.dumps({"community": {"autonomyMode": mode}}))

    return write


# ------------------------------------------------------------------ autonomy


def test_a_live_gate_follows_the_saved_mode_without_a_restart(config_file):
    gate = AutonomyGate("hitl", live=True)
    config_file("hitl")
    assert gate.requires_approval("comment")
    config_file("fully_autonomous")  # what the desktop app saves
    assert gate.mode == "fully_autonomous" and not gate.requires_approval("comment")
    config_file("hybrid")
    assert not gate.requires_approval("comment") and gate.requires_approval("post_proposal")


def test_a_live_gate_falls_back_when_the_file_is_unreadable(config_file, tmp_path):
    gate = AutonomyGate("hitl", live=True)
    (tmp_path / "config.json").write_text("{not json")
    assert gate.mode == "hitl"
    config_file("nonsense")
    assert gate.mode == "hitl"


def test_a_plain_gate_keeps_its_mode(config_file):
    config_file("fully_autonomous")
    assert AutonomyGate("hitl").requires_approval("comment")


def test_the_tools_use_a_live_gate():
    cfg = SimpleNamespace(enabled=True, api_base="http://x", agent_token="t", autonomy_mode="hitl")
    tools = community_tools.build_community_tools(cfg)
    assert tools and all(t._gate._live for t in tools if hasattr(t, "_gate"))


async def test_held_actions_say_the_user_approves_them(config_file):
    config_file("hitl")
    tool = community_tools.CommunityCommentTool(SimpleNamespace(), AutonomyGate("hitl", live=True))
    out = await tool.execute(thread_id="t", content="hi")
    assert "your user's approval" in out and "Community → Approvals" in out
    assert "Do not submit it again" in out and "a maintainer approves" not in out


def test_live_mode_reads_only_the_field(config_file, monkeypatch):
    config_file("hybrid")
    monkeypatch.setattr("mira_engine.config.loader.load_config", lambda *a, **k: pytest.fail("full config load"))
    assert autonomy.live_autonomy_mode() == "hybrid"


# ------------------------------------------------------------------ rules


def test_steps_the_engine_runs_are_not_standing_rules():
    text = render_rules_text(6, V6_ITEMS)
    assert "connection test" not in text.lower() and "Acknowledge the rules" not in text
    assert "Keep private data private" in text


def test_rules_cached_before_this_change_lose_those_steps_in_the_prompt():
    old = (
        "Mira Community Rules (v6) — ...:\n"
        "- [enforced] Complete the connection test: Before participating, reply once...\n"
        "- [enforced] Acknowledge the rules: Acknowledge the current rules version before writing to the community.\n"
        "- [enforced] Keep private data private: Never post secrets."
    )
    prompt = rules_for_prompt(old)
    assert "Complete the connection test" not in prompt and "Acknowledge the rules:" not in prompt
    assert "Keep private data private" in prompt and prompt.endswith(WELCOME_THREAD_NOTE)
    assert rules_for_prompt("") == ""


class _WelcomeClient:
    def __init__(self, status):
        self.status = status
        self.calls = []

    async def get_rules(self):
        self.calls.append("get_rules")
        return {"version": 6, "onboarding_thread_id": "welcome"}

    async def get_me(self):
        self.calls.append("get_me")
        return {"status": self.status}

    async def post_comment(self, thread_id, content, reply_to=None):
        self.calls.append(("post_comment", thread_id))
        return {"comment_id": "c1"}


async def test_an_active_agent_cannot_repeat_the_connection_test():
    client = _WelcomeClient("active")
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("hitl"))
    out = await tool.execute(thread_id="welcome", content="Connected. I acknowledge the rules v6.")
    assert out.startswith("Not posted") and ("post_comment", "welcome") not in client.calls
    # Nor is it queued for the user's approval.
    assert "approval" not in out


async def test_a_pending_agent_can_reply_in_the_welcome_thread():
    client = _WelcomeClient("pending")
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("fully_autonomous"))
    assert "Comment posted" in await tool.execute(thread_id="welcome", content="hi")


async def test_other_threads_skip_the_status_lookup():
    client = _WelcomeClient("active")
    tool = community_tools.CommunityCommentTool(client, AutonomyGate("fully_autonomous"))
    assert "Comment posted" in await tool.execute(thread_id="other", content="hi")
    assert "Comment posted" in await tool.execute(thread_id="other", content="hi again")
    assert client.calls.count("get_rules") == 1 and "get_me" not in client.calls


def test_community_turns_get_the_filtered_rules():
    from mira_engine.agent.base_loop import BaseAgentLoop
    from mira_engine.config.schema import ExecToolConfig, PythonRuntimeConfig

    loop = SimpleNamespace(
        exec_config=ExecToolConfig(python=PythonRuntimeConfig(manager="off")),
        community_config=SimpleNamespace(
            rules_text=render_rules_text(6, V6_ITEMS)
            + "\n- [enforced] Complete the connection test: x\n- [enforced] Acknowledge the rules: y"
        ),
    )
    out = BaseAgentLoop._compose_extra_system(loop, None, None, channel="community")
    assert "Complete the connection test" not in out and "Acknowledge the rules:" not in out
    assert WELCOME_THREAD_NOTE in out


# ------------------------------------------------------------------ name + onboarding


def _soul(tmp_path, name):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "SOUL.local.md").write_text(render_soul_local({"name": name, "identity": "一位严谨的科研助手"}), encoding="utf-8")
    return tmp_path


def test_configured_agent_name(tmp_path):
    assert configured_agent_name(tmp_path) is None
    assert configured_agent_name(None, _soul(tmp_path / "global", "MIRAI")) == "MIRAI"
    assert configured_agent_name(_soul(tmp_path / "project", "Ada"), tmp_path / "global") == "Ada"


def test_the_identity_uses_the_users_name(tmp_path, monkeypatch):
    from mira_engine.agent.context import ContextBuilder

    monkeypatch.setattr("mira_engine.config.paths.get_workspace_path", lambda *_a, **_k: tmp_path)
    assert "You are mira" in ContextBuilder(tmp_path)._get_identity()
    _soul(tmp_path, "MIRAI")
    identity = ContextBuilder(tmp_path)._get_identity()
    assert identity.startswith("# MIRAI") and "You are MIRAI" in identity and "Your user named you MIRAI" in identity


def test_the_welcome_reply_is_the_agents_own_words_with_one_required_sentence():
    prompt = onboarding_prompt("MIRAI", 6)
    assert 'exact sentence: "I acknowledge the Mira Community Rules v6."' in prompt
    assert "Your name is MIRAI" in prompt and "own words" in prompt and "Nothing else is required" in prompt
    assert "Your name is" not in onboarding_prompt(None, 6)
    assert '"I acknowledge the Mira Community Rules."' in onboarding_prompt(None, None)


async def test_build_onboarding_prompt_fetches_the_rules_version_when_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("mira_engine.config.paths.get_workspace_path", lambda *_a, **_k: _soul(tmp_path, "MIRAI"))

    class Client:
        async def get_rules(self):
            return {"version": 7, "rules": V6_ITEMS}

    cfg = SimpleNamespace(rules_version=0, api_base="http://x", agent_token="t")
    prompt = await build_onboarding_prompt(cfg, None, client=Client())
    assert "Rules v7." in prompt and "Your name is MIRAI" in prompt

    class Down:
        async def get_rules(self):
            raise OSError("down")

    assert '"I acknowledge the Mira Community Rules."' in await build_onboarding_prompt(cfg, None, client=Down())
    cfg.rules_version = 6
    assert "Rules v6." in await build_onboarding_prompt(cfg, None, client=Down())
