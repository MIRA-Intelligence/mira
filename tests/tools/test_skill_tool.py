"""The `skill` tool and the per-turn skill helpers built on it."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from mira_engine.agent.loop import AgentLoop
from mira_engine.agent.runner import AgentRunner, AgentRunSpec
from mira_engine.agent.skill_events import skill_read_details
from mira_engine.agent.skill_turn import (
    invoked_skill_names,
    prepare_turn_skills,
    record_invoked_skills,
    synthetic_skill_messages,
)
from mira_engine.agent.skills import SkillsLoader
from mira_engine.agent.tools.skill import SkillTool
from mira_engine.session.manager import Session


def _write(ws: Path, name: str, body: str) -> Path:
    path = ws / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


async def test_skill_tool_returns_base_dir_and_body(tmp_path: Path) -> None:
    path = _write(tmp_path, "docx", "---\ndescription: Word\n---\n\nUse python-docx.")
    tool = SkillTool(workspace=tmp_path)
    out = await tool.execute(skill="/docx", args="report.docx")
    assert out == f"Base directory for this skill: {path.parent}\n\nUse python-docx.\n\nARGUMENTS: report.docx"
    assert tool.read_only


async def test_skill_tool_errors(tmp_path: Path) -> None:
    _write(tmp_path, "docx", "---\ndescription: Word\n---\n\nbody")
    _write(tmp_path, "manual-only", "---\ndescription: M\ndisable-model-invocation: true\n---\n\nbody")
    tool = SkillTool(workspace=tmp_path)
    unknown = await tool.execute(skill="dox")
    assert unknown.startswith("Error: unknown skill 'dox'") and "docx" in unknown
    assert (await tool.execute(skill="manual-only")).startswith("Error:")


async def test_skill_tool_follows_project_context(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project, "proj-skill", "---\ndescription: P\n---\n\nproject body")
    tool = SkillTool(workspace=tmp_path / "global")
    assert (await tool.execute(skill="proj-skill")).startswith("Error:")
    tool.set_project_context("p1", str(project))
    assert (await tool.execute(skill="proj-skill")).endswith("project body")
    tool.clear_project_context()
    assert (await tool.execute(skill="proj-skill")).startswith("Error:")


def test_skill_tool_result_counts_as_loaded() -> None:
    event = skill_read_details("skill", {"skill": "docx"}, "Base directory for this skill: /x\n\nbody")
    assert event["skill_name"] == "docx" and event["loaded"] is True
    assert skill_read_details("skill", {"skill": "docx"}, "Error: unknown skill 'docx'.") is None


def test_invoked_names_ignore_failed_calls_and_record_order(tmp_path: Path) -> None:
    _write(tmp_path, "a", "---\ndescription: A\n---\n\nA body")
    _write(tmp_path, "b", "---\ndescription: B\n---\n\nB body")
    loader = SkillsLoader(tmp_path, builtin_skills_dir=None)
    messages = synthetic_skill_messages(loader, ["a", "missing", "b"])
    assert len(messages[0]["tool_calls"]) == 2
    failed = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "bad", "type": "function", "function": {"name": "skill", "arguments": json.dumps({"skill": "c"})},
        }]},
        {"role": "tool", "tool_call_id": "bad", "name": "skill", "content": "Error: unknown skill 'c'."},
    ]
    assert invoked_skill_names(messages + failed) == ["a", "b"]

    metadata = {"_invoked_skills": ["b", "old"]}
    record_invoked_skills(metadata, messages)
    assert metadata["_invoked_skills"] == ["old", "a", "b"]


def test_selected_skill_is_injected_once(tmp_path: Path) -> None:
    _write(tmp_path, "sheet", "---\ndescription: spreadsheet helper\n---\n\nsheet body")
    loader = SkillsLoader(tmp_path, builtin_skills_dir=None)
    meta = {"selected_skill_ids": ["sheet", "missing"]}
    first = prepare_turn_skills(loader, meta, "spreadsheet", recent=[], history=[])
    assert first.selected == ["sheet"] and first.injected == ["sheet"]
    assert first.suggested == []
    assert "MIRA_SKILL_FALLBACK_REQUIRED" in first.mandatory_hint
    history = [{"role": "user", "content": "spreadsheet"}, *first.synthetic_messages]
    again = prepare_turn_skills(loader, meta, "spreadsheet", recent=[], history=history)
    assert again.injected == [] and again.synthetic_messages == []
    assert again.mandatory_hint
    hint_only = prepare_turn_skills(loader, {}, "sheet spreadsheet", recent=[], history=[])
    assert hint_only.suggested == ["sheet"] and hint_only.synthetic_messages == []


def test_runner_keeps_skill_results_whole(tmp_path: Path) -> None:
    runner = AgentRunner(MagicMock())
    spec = AgentRunSpec(
        initial_messages=[], tools=MagicMock(), model="m", max_iterations=1,
        max_tool_result_chars=16_000, workspace=tmp_path, session_key="s",
    )
    body = "x" * 30_000
    assert runner._normalize_tool_result(spec, "c1", "skill", body) == body
    assert runner._normalize_tool_result(spec, "c2", "read_file", body) != body


def test_save_turn_keeps_skill_results_whole() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.max_tool_result_chars = 16_000
    session = Session(key="test:skill")
    body = "x" * 30_000
    loop._save_turn(session, [
        {"role": "tool", "tool_call_id": "1", "name": "skill", "content": body},
        {"role": "tool", "tool_call_id": "2", "name": "read_file", "content": body},
    ], skip=0)
    assert session.messages[0]["content"] == body
    assert session.messages[1]["content"].endswith("... (truncated)")
