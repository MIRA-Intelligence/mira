from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mira_engine.agent.hook import AgentHookContext
from mira_engine.agent.skill_events import skill_loaded, skill_read_details
from mira_engine.agent.skills import SkillsLoader
from mira_engine.agent.subagent import SubagentManager
from mira_engine.bus.queue import MessageBus
from mira_engine.providers.base import ToolCallRequest


@pytest.mark.parametrize("result", ["Error: not found", "Error reading file: denied", "(Empty file: x)", "", None])
def test_failed_reads_never_count(result):
    assert skill_read_details("read_file", {"path": "/skills/demo/SKILL.md"}, result) is None


def test_read_event_success_partial_and_windows():
    event = skill_read_details("read_file", {"path": r"C:\skills\demo\SKILL.md"}, "1| instructions")
    assert event["skill_name"] == "demo"
    assert event["source"] == "read_file"
    assert event["partial"] is False
    assert skill_read_details("read_file", {"path": "/demo/SKILL.md", "offset": 5}, "5| x")["partial"]
    assert skill_read_details("read_file", {"path": "/demo/SKILL.md"}, "1| x\n(Showing lines 1-1 of 2)")["partial"]
    assert skill_read_details("read_file", {"path": "/demo/README.md"}, "1| x") is None
    assert skill_read_details("exec", {"path": "/demo/SKILL.md"}, "1| x") is None
    assert skill_loaded("demo", "manual")["event_id"] != event["event_id"]


def test_collector_only_records_injected_content(tmp_path):
    for name, content in {"demo": "---\nname: demo\n---\nReal instructions", "empty": "---\nname: empty\n---\n"}.items():
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(content)
    loader = SkillsLoader(tmp_path, builtin_skills_dir=None)
    loaded = []
    prompt = loader.load_skills_for_context(["demo", "empty", "missing"], loaded_skills=loaded)
    assert loaded == ["demo"]
    assert "Real instructions" in prompt
    assert "empty" not in prompt


async def test_delegate_hook_and_context_isolation(tmp_path):
    callback = AsyncMock()
    manager = SubagentManager(SimpleNamespace(get_default_model=lambda: "test"), tmp_path, MessageBus())
    with manager.skill_audit_scope(callback):
        hook = await manager._skill_hook("critic:1", ["always-skill"])
        await hook.after_iteration(AgentHookContext(iteration=1, messages=[
            {"role": "tool", "tool_call_id": "1", "content": "1| instructions"},
            {"role": "tool", "tool_call_id": "2", "content": "Error: denied"},
        ], tool_calls=[
            ToolCallRequest(id="1", name="read_file", arguments={"path": "/demo/SKILL.md"}),
            ToolCallRequest(id="2", name="read_file", arguments={"path": "/bad/SKILL.md"}),
        ], tool_results=["1| instructions", "Error: denied"]))
    assert [call.args[0]["skill_name"] for call in callback.call_args_list] == ["always-skill", "demo"]
    assert all(call.args[0]["agent"] == "critic:1" for call in callback.call_args_list)
    assert await manager._skill_hook("critic:2", ["other"]) is None
