"""Tests for the opt-in ``pip install`` -> ``uv pip install`` rewrite.

The rewrite is gated on ``PythonRuntimeConfig.rewrite_pip_install`` and
only fires while a project venv is active. The text-substitution helper
is exercised in isolation; ExecTool integration is verified by patching
the low-level ``_spawn`` so we capture the actual command without the
warmup shell ``true`` invocation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mira_engine.agent.python_runtime_hint import build_python_runtime_hint
from mira_engine.agent.tools.shell import (
    ExecTool,
    rewrite_pip_install_to_uv,
)
from mira_engine.config.schema import PythonRuntimeConfig


# ---------------------------------------------------------------------------
# rewrite_pip_install_to_uv
# ---------------------------------------------------------------------------


class TestRewritePipInstall:

    @pytest.mark.parametrize(
        "before,after",
        [
            ("pip install foo", "uv pip install foo"),
            ("pip install -r requirements.txt", "uv pip install -r requirements.txt"),
            ("pip3 install foo", "uv pip install foo"),
            ("python -m pip install foo", "uv pip install foo"),
            ("python3 -m pip install foo bar", "uv pip install foo bar"),
        ],
    )
    def test_rewrites_canonical_forms(self, before: str, after: str) -> None:
        assert rewrite_pip_install_to_uv(before) == after

    @pytest.mark.parametrize(
        "command",
        [
            "pip list",
            "pip show requests",
            "pip freeze > out.txt",
            "pip --version",
            "python -m pip list",
            "python -m pip --version",
        ],
    )
    def test_does_not_rewrite_readonly_subcommands(self, command: str) -> None:
        assert rewrite_pip_install_to_uv(command) == command

    def test_preserves_env_prefix(self) -> None:
        assert (
            rewrite_pip_install_to_uv("PIP_INDEX_URL=https://x pip install foo")
            == "PIP_INDEX_URL=https://x uv pip install foo"
        )

    def test_handles_multiple_env_prefixes(self) -> None:
        assert (
            rewrite_pip_install_to_uv("A=1 B=2 pip install foo")
            == "A=1 B=2 uv pip install foo"
        )

    def test_rewrites_in_chained_commands(self) -> None:
        assert (
            rewrite_pip_install_to_uv("cd src && pip install -e .")
            == "cd src && uv pip install -e ."
        )

    def test_rewrites_each_segment_in_chain(self) -> None:
        assert (
            rewrite_pip_install_to_uv(
                "pip install foo && pip install bar"
            )
            == "uv pip install foo && uv pip install bar"
        )

    def test_rewrites_after_semicolon(self) -> None:
        assert (
            rewrite_pip_install_to_uv("echo go ; pip install foo")
            == "echo go ; uv pip install foo"
        )

    def test_does_not_rewrite_pip_inside_a_string_argument(self) -> None:
        # ``echo "pip install"`` shouldn't be rewritten — pip isn't the
        # invoked binary. The current implementation tolerates this
        # because the pattern requires a boundary (start, &&, ||, ;, |)
        # and the ``echo`` command shadows the leading boundary here.
        # We assert the *behaviour* — the embedded text is left alone.
        assert (
            rewrite_pip_install_to_uv('echo "pip install foo"')
            == 'echo "pip install foo"'
        )

    def test_handles_path_prefixed_pip(self) -> None:
        assert (
            rewrite_pip_install_to_uv("./.venv/bin/pip install foo")
            == "uv pip install foo"
        )

    def test_no_op_for_non_pip_commands(self) -> None:
        for cmd in ("python script.py", "ls -la", "git status", ""):
            assert rewrite_pip_install_to_uv(cmd) == cmd


# ---------------------------------------------------------------------------
# Prompt hint integration (PR 5 + PR 9)
# ---------------------------------------------------------------------------


class TestPromptHintWithRewrite:

    def test_omits_rewrite_note_when_disabled(self) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        hint = build_python_runtime_hint(cfg)
        assert hint is not None
        assert "automatically rewritten" not in hint

    def test_includes_rewrite_note_when_enabled(self) -> None:
        cfg = PythonRuntimeConfig(manager="uv", rewrite_pip_install=True)
        hint = build_python_runtime_hint(cfg)
        assert hint is not None
        assert "automatically rewritten to `uv pip install`" in hint
        # Read-only subcommands must be called out so the agent doesn't
        # think it can't inspect installed packages.
        assert "pip list" in hint


# ---------------------------------------------------------------------------
# ExecTool integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExecToolPipRewrite:

    @staticmethod
    def _tool(rewrite: bool, *, manager: str = "uv") -> ExecTool:
        cfg = PythonRuntimeConfig(manager=manager, rewrite_pip_install=rewrite)
        return ExecTool(timeout=5, python_runtime=cfg)

    async def test_should_rewrite_pip_off_by_default(self, tmp_path: Path) -> None:
        tool = self._tool(rewrite=False)
        assert tool._should_rewrite_pip() is False

    async def test_should_rewrite_pip_on_when_enabled(self) -> None:
        tool = self._tool(rewrite=True)
        assert tool._should_rewrite_pip() is True

    async def test_should_rewrite_pip_off_when_manager_off(self) -> None:
        tool = self._tool(rewrite=True, manager="off")
        assert tool._should_rewrite_pip() is False

    @staticmethod
    def _spawn_capture(observed: dict[str, str]):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        async def _capture(cmd, cwd, env):
            observed["cmd"] = cmd
            return mock_proc

        return _capture

    async def test_execute_rewrites_when_enabled(self, tmp_path: Path) -> None:
        tool = self._tool(rewrite=True)
        venv = tmp_path / ".venv"
        venv.mkdir()

        observed: dict[str, str] = {}
        with (
            patch.object(ExecTool, "_bootstrap_venv_sync", return_value=venv),
            patch.object(
                ExecTool, "_spawn", side_effect=self._spawn_capture(observed)
            ),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            await tool.execute("pip install requests", working_dir=str(tmp_path))

        assert observed["cmd"] == "uv pip install requests"

    async def test_execute_does_not_rewrite_when_disabled(
        self, tmp_path: Path
    ) -> None:
        tool = self._tool(rewrite=False)
        venv = tmp_path / ".venv"
        venv.mkdir()

        observed: dict[str, str] = {}
        with (
            patch.object(ExecTool, "_bootstrap_venv_sync", return_value=venv),
            patch.object(
                ExecTool, "_spawn", side_effect=self._spawn_capture(observed)
            ),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            await tool.execute("pip install requests", working_dir=str(tmp_path))

        assert observed["cmd"] == "pip install requests"

    async def test_execute_does_not_rewrite_without_venv(
        self, tmp_path: Path
    ) -> None:
        # ``rewrite_pip_install`` is on but bootstrap returns None
        # (e.g. uv missing): the rewrite is skipped because there's no
        # venv to route to anyway.
        tool = self._tool(rewrite=True)
        observed: dict[str, str] = {}
        with (
            patch.object(ExecTool, "_bootstrap_venv_sync", return_value=None),
            patch.object(
                ExecTool, "_spawn", side_effect=self._spawn_capture(observed)
            ),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            await tool.execute("pip install requests", working_dir=str(tmp_path))

        assert observed["cmd"] == "pip install requests"

    async def test_execute_rewrites_chained_pip(self, tmp_path: Path) -> None:
        tool = self._tool(rewrite=True)
        venv = tmp_path / ".venv"
        venv.mkdir()

        observed: dict[str, str] = {}
        with (
            patch.object(ExecTool, "_bootstrap_venv_sync", return_value=venv),
            patch.object(
                ExecTool, "_spawn", side_effect=self._spawn_capture(observed)
            ),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            await tool.execute(
                "cd src && pip install -e . && python -m pytest",
                working_dir=str(tmp_path),
            )

        assert observed["cmd"] == "cd src && uv pip install -e . && python -m pytest"
