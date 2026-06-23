"""Tests for the Python-runtime system-prompt hint.

Verifies that ``build_python_runtime_hint`` and
``BaseAgentLoop._compose_extra_system`` only emit venv-related instructions
when ``tools.exec.python.manager`` is active. Default config
(``manager == 'off'``) must keep the system prompt byte-identical to
today's behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.python_runtime_hint import build_python_runtime_hint
from mira_engine.config.schema import ExecToolConfig, PythonRuntimeConfig


class TestBuildPythonRuntimeHint:

    def test_returns_none_when_manager_off(self) -> None:
        assert build_python_runtime_hint(PythonRuntimeConfig()) is None

    def test_returns_none_when_runtime_is_none(self) -> None:
        assert build_python_runtime_hint(None) is None

    def test_returns_none_when_object_has_no_manager(self) -> None:
        # Defensive: pre-PR-2 callers may pass an opaque object.
        assert build_python_runtime_hint(SimpleNamespace()) is None

    def test_emits_section_when_manager_uv(self) -> None:
        hint = build_python_runtime_hint(PythonRuntimeConfig(manager="uv"))
        assert hint is not None
        assert hint.startswith("## Python environment")
        # Mentions the standard idioms agent should use.
        assert "uv pip install" in hint
        assert ".venv" in hint
        # Discourages bare pip.
        assert "Do **not** call `pip install` directly" in hint

    def test_section_includes_pinned_python_version(self) -> None:
        hint = build_python_runtime_hint(
            PythonRuntimeConfig(manager="uv", python_version="3.11.10")
        )
        assert hint is not None
        assert "3.11.10" in hint

    def test_section_omits_python_version_line_when_unset(self) -> None:
        hint = build_python_runtime_hint(PythonRuntimeConfig(manager="uv"))
        assert hint is not None
        assert "interpreter is pinned" not in hint

    def test_section_includes_baseline_requirements(self) -> None:
        hint = build_python_runtime_hint(
            PythonRuntimeConfig(
                manager="uv", baseline_requirements=["numpy", "pandas"]
            )
        )
        assert hint is not None
        assert "`numpy`" in hint
        assert "`pandas`" in hint

    def test_section_omits_baseline_line_when_empty(self) -> None:
        hint = build_python_runtime_hint(PythonRuntimeConfig(manager="uv"))
        assert hint is not None
        assert "Pre-installed baseline" not in hint

    def test_section_uses_configured_venv_dir(self) -> None:
        hint = build_python_runtime_hint(
            PythonRuntimeConfig(manager="uv", venv_dir=".envs/proj-A")
        )
        assert hint is not None
        assert ".envs/proj-A" in hint


class TestComposeExtraSystem:
    """Smoke-test the merge: instance method on a stand-in object so we
    don't pull in BaseAgentLoop's heavy dependency graph."""

    @staticmethod
    def _loop(exec_config: ExecToolConfig) -> SimpleNamespace:
        return SimpleNamespace(exec_config=exec_config)

    def test_no_python_hint_when_manager_off(self) -> None:
        loop = self._loop(ExecToolConfig())
        result = BaseAgentLoop._compose_extra_system(loop, "ui-instr", "guard")
        assert result is not None
        assert "## Python environment" not in result
        assert result == "ui-instr\n\nguard"

    def test_python_hint_prepended_when_manager_uv(self) -> None:
        cfg = ExecToolConfig(python=PythonRuntimeConfig(manager="uv"))
        loop = self._loop(cfg)
        result = BaseAgentLoop._compose_extra_system(loop, "ui-instr", "guard")
        assert result is not None
        assert result.startswith("## Python environment")
        # Subsequent sections separated by blank lines.
        assert "\n\nui-instr\n\nguard" in result

    def test_returns_none_when_everything_empty_and_manager_off(self) -> None:
        loop = self._loop(ExecToolConfig())
        assert BaseAgentLoop._compose_extra_system(loop, None, None) is None

    def test_returns_only_python_hint_when_others_empty(self) -> None:
        cfg = ExecToolConfig(python=PythonRuntimeConfig(manager="uv"))
        loop = self._loop(cfg)
        result = BaseAgentLoop._compose_extra_system(loop, None, "")
        assert result is not None
        assert result.startswith("## Python environment")
        assert not result.endswith("\n\n")

    def test_handles_non_string_inputs(self) -> None:
        """Pre-existing contract: opaque inputs are coerced/ignored."""
        loop = self._loop(ExecToolConfig())
        assert BaseAgentLoop._compose_extra_system(loop, 12345, None) is None

    def test_handles_missing_python_attr_on_exec_config(self) -> None:
        """If somehow an ExecToolConfig-like object lacks ``python``,
        the helper falls back to no-op rather than raising."""
        loop = SimpleNamespace(exec_config=SimpleNamespace())
        result = BaseAgentLoop._compose_extra_system(loop, "ui", "g")
        assert result == "ui\n\ng"


class TestCommunityRulesInjection:
    """The cached community rules (#33) are injected only on community turns."""

    @staticmethod
    def _loop(rules_text: str) -> SimpleNamespace:
        return SimpleNamespace(
            exec_config=ExecToolConfig(),
            community_config=SimpleNamespace(rules_text=rules_text),
        )

    def test_rules_injected_for_community_channel(self) -> None:
        loop = self._loop("Mira Community Rules (v2) — follow them.")
        result = BaseAgentLoop._compose_extra_system(
            loop, "ui", "guard", channel="community"
        )
        assert result is not None
        assert "Mira Community Rules (v2)" in result

    def test_rules_not_injected_for_other_channels(self) -> None:
        loop = self._loop("SECRET RULES")
        result = BaseAgentLoop._compose_extra_system(
            loop, "ui", "guard", channel="telegram"
        )
        assert result == "ui\n\nguard"

    def test_no_channel_means_no_rules(self) -> None:
        loop = self._loop("SECRET RULES")
        result = BaseAgentLoop._compose_extra_system(loop, "ui", "guard")
        assert result == "ui\n\nguard"

    def test_community_channel_without_rules_text_is_noop(self) -> None:
        loop = self._loop("")
        result = BaseAgentLoop._compose_extra_system(
            loop, "ui", "guard", channel="community"
        )
        assert result == "ui\n\nguard"
