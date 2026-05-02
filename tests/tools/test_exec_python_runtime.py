"""Tests for ExecTool's per-project Python runtime integration.

These tests exercise the wiring added in PR 4 of the
``Per-project Python environments`` milestone:

- ``_is_python_command`` heuristic detects python-ish commands.
- ``_apply_venv_to_env`` mutates env in the same way as ``activate``.
- ``_maybe_bootstrap_venv`` only runs when the manager is ``uv``,
  caches results, and degrades gracefully when bootstrap fails.
- ``execute()`` calls into the bootstrap path and the resulting
  subprocess env carries ``VIRTUAL_ENV`` + venv-bin-prefixed PATH.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mira_engine.agent.tools import shell as shell_module
from mira_engine.agent.tools.shell import ExecTool, _is_python_command
from mira_engine.config.schema import PythonRuntimeConfig


# ---------------------------------------------------------------------------
# _is_python_command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python script.py",
        "python3 -m pytest",
        "pip install numpy",
        "pip3 install -e .",
        "pytest",
        "ipython",
        "jupyter notebook",
        "uv pip install foo",
        "/usr/bin/python script.py",
        "/opt/conda/envs/x/bin/python3 train.py",
        "cd /tmp && python x.py",
        "echo hi; pip install bar",
        "true | python -c 'print(1)'",
        "PYTHONHASHSEED=0 python script.py",  # leading env-var prefix
    ],
)
def test_is_python_command_positive(command: str) -> None:
    assert _is_python_command(command), command


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "echo python",          # python only as data, not the executable
        "git pip-compile --help",
        "man pytest",            # passing pytest as argument to man
        "rm -rf .venv",
        "",
    ],
)
def test_is_python_command_negative(command: str) -> None:
    assert not _is_python_command(command), command


# ---------------------------------------------------------------------------
# _apply_venv_to_env
# ---------------------------------------------------------------------------


class TestApplyVenvToEnv:

    def test_prepends_venv_bin_to_path_unix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shell_module, "_IS_WINDOWS", False)
        env = {"PATH": "/usr/local/bin:/usr/bin"}
        ExecTool._apply_venv_to_env(env, tmp_path / ".venv")
        assert env["PATH"].startswith(str(tmp_path / ".venv" / "bin") + ":")
        assert env["PATH"].endswith("/usr/local/bin:/usr/bin")

    def test_prepends_venv_scripts_on_windows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shell_module, "_IS_WINDOWS", True)
        env = {"PATH": r"C:\Windows\System32"}
        ExecTool._apply_venv_to_env(env, tmp_path / ".venv")
        assert env["PATH"].startswith(str(tmp_path / ".venv" / "Scripts") + ";")
        assert r"C:\Windows\System32" in env["PATH"]

    def test_sets_virtual_env(self, tmp_path: Path) -> None:
        env: dict[str, str] = {}
        ExecTool._apply_venv_to_env(env, tmp_path / ".venv")
        assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")

    def test_handles_empty_path(self, tmp_path: Path) -> None:
        env: dict[str, str] = {}
        ExecTool._apply_venv_to_env(env, tmp_path / ".venv")
        # PATH must still be set (subprocesses need it) and contain only the venv bin.
        assert "PATH" in env
        assert env["PATH"]

    def test_scrubs_conda_and_pythonhome(self, tmp_path: Path) -> None:
        env = {
            "PATH": "/usr/bin",
            "CONDA_PREFIX": "/opt/conda/envs/foo",
            "CONDA_DEFAULT_ENV": "foo",
            "PYTHONHOME": "/usr/local/python",
        }
        ExecTool._apply_venv_to_env(env, tmp_path / ".venv")
        assert "CONDA_PREFIX" not in env
        assert "CONDA_DEFAULT_ENV" not in env
        assert "PYTHONHOME" not in env


# ---------------------------------------------------------------------------
# _maybe_bootstrap_venv
# ---------------------------------------------------------------------------


class TestMaybeBootstrapVenv:

    @pytest.mark.asyncio
    async def test_returns_none_when_runtime_disabled(self, tmp_path: Path) -> None:
        tool = ExecTool(python_runtime=None)
        assert await tool._maybe_bootstrap_venv("python x.py", str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_manager_off(self, tmp_path: Path) -> None:
        tool = ExecTool(python_runtime=PythonRuntimeConfig(manager="off"))
        assert await tool._maybe_bootstrap_venv("python x.py", str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_auto_bootstrap_disabled(
        self, tmp_path: Path
    ) -> None:
        cfg = PythonRuntimeConfig(manager="uv", auto_bootstrap=False)
        tool = ExecTool(python_runtime=cfg)
        assert await tool._maybe_bootstrap_venv("python x.py", str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_python_command(self, tmp_path: Path) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        tool = ExecTool(python_runtime=cfg)
        with patch.object(
            ExecTool, "_bootstrap_venv_sync", side_effect=AssertionError("must not run")
        ):
            assert await tool._maybe_bootstrap_venv("ls -la", str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_bootstraps_for_python_command_and_caches(
        self, tmp_path: Path
    ) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        tool = ExecTool(python_runtime=cfg)
        venv = tmp_path / ".venv"
        with patch.object(
            ExecTool, "_bootstrap_venv_sync", return_value=venv
        ) as mock_bootstrap:
            result1 = await tool._maybe_bootstrap_venv("python x.py", str(tmp_path))
            result2 = await tool._maybe_bootstrap_venv("pytest", str(tmp_path))
        assert result1 == venv
        assert result2 == venv
        # Bootstrap must be idempotent: only the first call runs the helper.
        assert mock_bootstrap.call_count == 1

    @pytest.mark.asyncio
    async def test_caches_negative_result_on_failure(
        self, tmp_path: Path, caplog
    ) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        tool = ExecTool(python_runtime=cfg)
        with patch.object(
            ExecTool, "_bootstrap_venv_sync", side_effect=RuntimeError("uv missing")
        ) as mock_bootstrap:
            with caplog.at_level("WARNING"):
                first = await tool._maybe_bootstrap_venv("python x.py", str(tmp_path))
                second = await tool._maybe_bootstrap_venv("python y.py", str(tmp_path))
        assert first is None
        assert second is None
        assert mock_bootstrap.call_count == 1
        assert "uv missing" in caplog.text

    @pytest.mark.asyncio
    async def test_cache_keyed_per_directory(self, tmp_path: Path) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        tool = ExecTool(python_runtime=cfg)
        proj_a = tmp_path / "A"
        proj_b = tmp_path / "B"
        proj_a.mkdir()
        proj_b.mkdir()

        def _fake(cwd: str, runtime):
            return Path(cwd) / ".venv"

        with patch.object(
            ExecTool, "_bootstrap_venv_sync", side_effect=_fake
        ) as mock_bootstrap:
            result_a = await tool._maybe_bootstrap_venv("python x.py", str(proj_a))
            result_b = await tool._maybe_bootstrap_venv("python x.py", str(proj_b))
        assert result_a == proj_a / ".venv"
        assert result_b == proj_b / ".venv"
        assert mock_bootstrap.call_count == 2


# ---------------------------------------------------------------------------
# execute() integration: env carries the venv when bootstrap succeeds
# ---------------------------------------------------------------------------


class TestExecuteUsesVenv:

    @pytest.mark.asyncio
    async def test_subprocess_env_carries_venv_when_python_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        venv = tmp_path / ".venv"

        captured_env: dict[str, str] = {}
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        async def _capture_spawn(cmd, cwd, env):
            captured_env.update(env)
            return mock_proc

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", False),
            patch.object(ExecTool, "_bootstrap_venv_sync", return_value=venv),
            patch.object(ExecTool, "_spawn", side_effect=_capture_spawn),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(python_runtime=cfg, working_dir=str(tmp_path))
            await tool.execute(command="python --version")

        assert captured_env.get("VIRTUAL_ENV") == str(venv)
        assert captured_env.get("PATH", "").startswith(
            str(venv / "bin") + ":"
        ) or captured_env.get("PATH", "") == str(venv / "bin")

    @pytest.mark.asyncio
    async def test_subprocess_env_unchanged_for_non_python_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        captured_env: dict[str, str] = {}
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        async def _capture_spawn(cmd, cwd, env):
            captured_env.update(env)
            return mock_proc

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", False),
            patch.object(
                ExecTool, "_bootstrap_venv_sync", side_effect=AssertionError("nope")
            ),
            patch.object(ExecTool, "_spawn", side_effect=_capture_spawn),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(python_runtime=cfg, working_dir=str(tmp_path))
            await tool.execute(command="ls -la")

        assert "VIRTUAL_ENV" not in captured_env
