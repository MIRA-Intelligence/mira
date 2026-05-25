from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.skill_plugins import SkillPluginManager
from mira_engine.agent.skills import SkillsLoader
from mira_engine.agent import skill_plugins as skill_plugins_mod

TAG = ContextBuilder._RUNTIME_CONTEXT_TAG


def _tc(cid: str, name: str = "fn") -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _tool(cid: str, content: str = "ok", name: str = "fn") -> dict:
    return {"role": "tool", "tool_call_id": cid, "name": name, "content": content}


class TestSanitizeToolPairs:
    def test_preserves_complete_single_pair(self) -> None:
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
            _tool("a"),
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == msgs

    def test_preserves_multiple_calls_all_results(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [_tc("1", "f1"), _tc("2", "f2")],
            },
            _tool("1", "r1", "f1"),
            _tool("2", "r2", "f2"),
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == msgs

    def test_strips_single_missing_result_with_content(self) -> None:
        msgs = [
            {"role": "assistant", "content": "keep me", "tool_calls": [_tc("a")]},
            {"role": "user", "content": "hi"},
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == [
            {"role": "assistant", "content": "keep me"},
            {"role": "user", "content": "hi"},
        ]

    def test_strips_missing_result_preserves_reasoning_metadata(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "keep me",
                "reasoning_content": "hidden reasoning",
                "thinking_blocks": [{"type": "thinking", "signature": "sig"}],
                "tool_calls": [_tc("a")],
            },
            {"role": "user", "content": "hi"},
        ]

        assert ContextBuilder._sanitize_tool_pairs(msgs) == [
            {
                "role": "assistant",
                "content": "keep me",
                "reasoning_content": "hidden reasoning",
                "thinking_blocks": [{"type": "thinking", "signature": "sig"}],
            },
            {"role": "user", "content": "hi"},
        ]

    def test_strips_partial_results(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "c",
                "tool_calls": [_tc("1"), _tc("2")],
            },
            _tool("1"),
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == [{"role": "assistant", "content": "c"}]

    def test_removes_assistant_no_content_missing_results(self) -> None:
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
            {"role": "user", "content": "u"},
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == [{"role": "user", "content": "u"}]

    def test_removes_assistant_none_content_missing_results(self) -> None:
        msgs = [
            {"role": "assistant", "content": None, "tool_calls": [_tc("a")]},
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == []

    def test_unrelated_tool_ids_do_not_satisfy_pair(self) -> None:
        msgs = [
            {"role": "assistant", "content": "x", "tool_calls": [_tc("wanted")]},
            _tool("other", "orphan"),
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == [{"role": "assistant", "content": "x"}]

    def test_empty_messages(self) -> None:
        assert ContextBuilder._sanitize_tool_pairs([]) == []

    def test_system_and_user_unchanged(self) -> None:
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == msgs

    def test_assistant_without_tool_calls_unchanged(self) -> None:
        msgs = [{"role": "assistant", "content": "hello"}]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == msgs

    def test_mixed_valid_then_orphan_block(self) -> None:
        first = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("ok")]},
            _tool("ok"),
        ]
        second = [
            {"role": "assistant", "content": "bad", "tool_calls": [_tc("missing")]},
        ]
        msgs = [*first, *second]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == [
            *first,
            {"role": "assistant", "content": "bad"},
        ]

    def test_non_dict_tool_call_entries_ignored_for_expected_ids(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "z",
                "tool_calls": ["not-a-dict", _tc("real"), None],
            },
            _tool("real"),
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == msgs

    def test_all_non_dict_tool_calls_strips_to_content(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": "only text",
                "tool_calls": ["x", 1, None],
            },
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == [{"role": "assistant", "content": "only text"}]

    def test_all_non_dict_no_content_removed(self) -> None:
        msgs = [{"role": "assistant", "content": "", "tool_calls": ["x"]}]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == []

    def test_interleaved_unrelated_tool_then_valid_still_preserves_pair(self) -> None:
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
            _tool("orphan", "nope"),
            _tool("a", "yes"),
        ]
        want = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
            _tool("a", "yes"),
        ]
        assert ContextBuilder._sanitize_tool_pairs(msgs) == want

    def test_extra_duplicate_tool_result_for_same_id(self) -> None:
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("a")]},
            _tool("a", "first"),
            _tool("a", "second"),
        ]
        out = ContextBuilder._sanitize_tool_pairs(msgs)
        assert out[0] == msgs[0]
        assert out[1:] == msgs[1:]


class TestBuildRuntimeContext:
    def test_channel_and_chat_id(self) -> None:
        with (
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="TZ"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            s = ContextBuilder._build_runtime_context("discord", "c1", None)
        assert s.startswith(TAG + "\n")
        assert "Current Time: T (TZ)" in s
        assert "Channel: discord" in s
        assert "Chat ID: c1" in s
        assert "Project Directory" not in s

    def test_project_dir(self) -> None:
        with (
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            s = ContextBuilder._build_runtime_context("x", "y", "/abs/proj")
        assert "Project Directory: /abs/proj" in s

    def test_ui_default_project_dir(self) -> None:
        with (
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            s = ContextBuilder._build_runtime_context("ui", "abc123", None)
        assert "Project Directory: projects/abc123" in s

    def test_no_channel_or_chat_id_time_only(self) -> None:
        with (
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            s = ContextBuilder._build_runtime_context(None, None, None)
        assert s == TAG + "\nCurrent Time: T (UTC)"

    def test_partial_channel_missing_chat_id(self) -> None:
        with (
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            s = ContextBuilder._build_runtime_context("ui", None, None)
        assert s == TAG + "\nCurrent Time: T (UTC)"


@patch("mira_engine.agent.context.ContextBuilder._load_builtin_template")
class TestLoadBootstrapFiles:
    def test_workspace_override(self, mock_builtin: MagicMock, tmp_path: Path) -> None:
        mock_builtin.return_value = None
        (tmp_path / "AGENTS.md").write_text("WS agents\n", encoding="utf-8")
        cb = ContextBuilder(tmp_path)
        out = cb._load_bootstrap_files()
        assert "## AGENTS.md" in out
        assert "WS agents" in out
        mock_builtin.assert_called()

    def test_fallback_builtin_when_missing_workspace_file(
        self, mock_builtin: MagicMock, tmp_path: Path,
    ) -> None:
        mock_builtin.side_effect = lambda fn: f"BUILTIN-{fn}"
        cb = ContextBuilder(tmp_path)
        out = cb._load_bootstrap_files()
        for name in ContextBuilder.BOOTSTRAP_FILES:
            assert f"## {name}" in out
            assert f"BUILTIN-{name}" in out

    def test_local_md_appended(self, mock_builtin: MagicMock, tmp_path: Path) -> None:
        mock_builtin.return_value = "base"
        (tmp_path / "AGENTS.md").write_text("base", encoding="utf-8")
        (tmp_path / "AGENTS.local.md").write_text("extra bit", encoding="utf-8")
        for other in ("SOUL.md", "USER.md", "TOOLS.md"):
            (tmp_path / other).write_text("x", encoding="utf-8")
        cb = ContextBuilder(tmp_path)
        out = cb._load_bootstrap_files()
        assert "base\n\nextra bit" in out

    def test_empty_content_skipped(self, mock_builtin: MagicMock, tmp_path: Path) -> None:
        mock_builtin.return_value = "   \n"
        cb = ContextBuilder(tmp_path)
        assert cb._load_bootstrap_files() == ""

    def test_switches_agents_template_file(
        self, mock_builtin: MagicMock, tmp_path: Path,
    ) -> None:
        mock_builtin.side_effect = lambda fn: f"BUILTIN-{fn}"
        cb = ContextBuilder(tmp_path)
        out = cb._load_bootstrap_files(agents_filename="AGENTS_EG.md")
        assert "## AGENTS_EG.md" in out
        assert "BUILTIN-AGENTS_EG.md" in out
        assert "## AGENTS.md" not in out


@patch("mira_engine.agent.context.SkillsLoader")
@patch("mira_engine.agent.context.MemoryStore")
class TestBuildMessages:
    def test_text_merged_with_runtime_context(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        with (
            patch.object(ContextBuilder, "build_system_prompt", return_value="SYS"),
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            cb = ContextBuilder(tmp_path)
            out = cb.build_messages([], "hello", channel="c", chat_id="id")
        assert len(out) == 2
        assert out[0] == {"role": "system", "content": "SYS"}
        user = out[1]["content"]
        assert isinstance(user, str)
        assert user.startswith(TAG)
        assert user.endswith("hello")
        assert "\n\n" in user

    def test_extra_system_appended(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        with (
            patch.object(ContextBuilder, "build_system_prompt", return_value="SYS"),
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            cb = ContextBuilder(tmp_path)
            out = cb.build_messages([], "x", extra_system="MORE")
        assert out[0]["content"] == "SYS\n\n---\n\nMORE"

    def test_history_orphan_tool_calls_sanitized(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        with (
            patch.object(ContextBuilder, "build_system_prompt", return_value="SYS"),
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            cb = ContextBuilder(tmp_path)
            history = [
                {"role": "assistant", "content": "k", "tool_calls": [_tc("nope")]},
            ]
            out = cb.build_messages(history, "q")
        assert out[1] == {"role": "assistant", "content": "k"}

    def test_agents_filename_forwarded_to_system_prompt(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        with (
            patch.object(ContextBuilder, "build_system_prompt", return_value="SYS") as mock_sp,
            patch("mira_engine.agent.context.datetime") as m_dt,
            patch("mira_engine.agent.context.time.strftime", return_value="UTC"),
        ):
            m_dt.now.return_value.strftime.return_value = "T"
            cb = ContextBuilder(tmp_path)
            cb.build_messages([], "hello", agents_filename="AGENTS_RS.md")
        _, kwargs = mock_sp.call_args
        assert kwargs["agents_filename"] == "AGENTS_RS.md"


@patch("mira_engine.agent.context.SkillsLoader")
@patch("mira_engine.agent.context.MemoryStore")
class TestAddToolResultAndAssistant:
    def test_add_tool_result(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        cb = ContextBuilder(tmp_path)
        msgs: list = [{"role": "user", "content": "u"}]
        r = cb.add_tool_result(msgs, "id1", "tool_x", "body")
        assert r is msgs
        assert msgs[-1] == {
            "role": "tool",
            "tool_call_id": "id1",
            "name": "tool_x",
            "content": "body",
        }

    def test_add_assistant_message_basic_and_optionals(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        cb = ContextBuilder(tmp_path)
        msgs: list = []
        cb.add_assistant_message(msgs, "hi")
        assert msgs[-1] == {"role": "assistant", "content": "hi"}

        cb.add_assistant_message(
            msgs,
            None,
            tool_calls=[_tc("z")],
            reasoning_content="r",
            thinking_blocks=[{"t": 1}],
        )
        assert msgs[-1] == {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tc("z")],
            "reasoning_content": "r",
            "thinking_blocks": [{"t": 1}],
        }


@patch("mira_engine.agent.context.SkillsLoader")
@patch("mira_engine.agent.context.MemoryStore")
class TestBuildUserContent:
    def test_no_media_plain_text(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        cb = ContextBuilder(tmp_path)
        assert cb._build_user_content("plain", None) == "plain"
        assert cb._build_user_content("plain", []) == "plain"

    def test_valid_image_file(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        p = tmp_path / "x.png"
        p.write_bytes(png_header)
        cb = ContextBuilder(tmp_path)
        out = cb._build_user_content("caption", [str(p)])
        assert isinstance(out, list)
        assert out[-1] == {"type": "text", "text": "caption"}
        img = out[0]
        assert img["type"] == "image_url"
        url = img["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == png_header

    def test_missing_file_plain_text(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        cb = ContextBuilder(tmp_path)
        assert cb._build_user_content("t", [str(tmp_path / "nope.png")]) == "t"

    def test_non_image_file_plain_text(
        self, _mock_mem: MagicMock, _mock_skills: MagicMock, tmp_path: Path,
    ) -> None:
        f = tmp_path / "doc.txt"
        f.write_text("hello", encoding="utf-8")
        cb = ContextBuilder(tmp_path)
        assert cb._build_user_content("t", [str(f)]) == "t"


def test_build_system_prompt_hides_disabled_plugin_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_workspace = tmp_path / "global-workspace"
    global_workspace.mkdir(parents=True)
    monkeypatch.setattr(skill_plugins_mod, "get_workspace_path", lambda _workspace: global_workspace)

    project_workspace = tmp_path / "project"
    plugin_source = tmp_path / "plugin-src"
    skill_dir = plugin_source / "skills" / "hidden-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: Hidden Skill\n---\n\n# hidden",
        encoding="utf-8",
    )
    (plugin_source / "plugin.json").write_text(
        json.dumps({
            "id": "hidden-pack",
            "version": "0.1.0",
            "skills": [{"id": "hidden-skill", "path": "skills/hidden-skill"}],
        }),
        encoding="utf-8",
    )

    manager = SkillPluginManager(project_workspace)
    manager.install_from_directory(plugin_source)

    cb = ContextBuilder(project_workspace)
    cb.skills = SkillsLoader(project_workspace, builtin_skills_dir=None, plugin_manager=manager)

    visible_prompt = cb.build_system_prompt()
    assert "<name>hidden-skill</name>" in visible_prompt
    assert cb.skills.load_skill("hidden-skill") is not None

    manager.set_enabled(
        scope="project",
        plugin_id="hidden-pack",
        target_type="skill",
        target_id="hidden-skill",
        enabled=False,
    )
    hidden_prompt = cb.build_system_prompt()
    assert "<name>hidden-skill</name>" not in hidden_prompt
    assert cb.skills.load_skill("hidden-skill") is None
