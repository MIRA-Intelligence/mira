from mira_engine.agent.loop import AgentLoop


def test_build_skill_invoked_event_for_skill_file_path() -> None:
    event = AgentLoop._build_skill_invoked_event(
        tool_name="read_file",
        arguments={"path": "/Users/demo/.mira/skills/research/scientific-method/SKILL.md"},
    )

    assert event == {
        "tool": "read_file",
        "skill_name": "scientific-method",
        "path": "/Users/demo/.mira/skills/research/scientific-method/SKILL.md",
    }


def test_build_skill_invoked_event_ignores_non_skill_paths() -> None:
    not_skill = AgentLoop._build_skill_invoked_event(
        tool_name="read_file",
        arguments={"path": "/Users/demo/project/README.md"},
    )
    non_read_file = AgentLoop._build_skill_invoked_event(
        tool_name="write_file",
        arguments={"path": "/Users/demo/.mira/skills/research/scientific-method/SKILL.md"},
    )

    assert not_skill is None
    assert non_read_file is None
