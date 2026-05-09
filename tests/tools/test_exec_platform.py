"""Tests for cross-platform shell execution.

Verifies that ExecTool selects the correct shell, environment, path-append
strategy, and sandbox behaviour per platform — without actually running
platform-specific binaries (all subprocess calls are mocked).
"""

from unittest.mock import AsyncMock, patch

import pytest

from mira_engine.agent.tools.shell import ExecTool

_WINDOWS_ENV_KEYS = {
    "APPDATA", "LOCALAPPDATA", "ProgramData",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
}


# ---------------------------------------------------------------------------
# _build_env
# ---------------------------------------------------------------------------

class TestBuildEnvUnix:

    def test_baseline_keys_present(self):
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        # Locale + home are always present even with an empty parent environ.
        assert {"HOME", "LANG", "TERM"} <= set(env)

    def test_home_from_environ(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/dev")
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        assert env["HOME"] == "/Users/dev"

    def test_runtime_keys_forwarded_when_set(self, monkeypatch):
        """PATH, VIRTUAL_ENV, CONDA_PREFIX, PYTHONPATH must reach subprocesses."""
        monkeypatch.setenv("PATH", "/opt/x:/usr/bin")
        monkeypatch.setenv("VIRTUAL_ENV", "/tmp/venv")
        monkeypatch.setenv("CONDA_PREFIX", "/opt/conda/envs/foo")
        monkeypatch.setenv("CONDA_DEFAULT_ENV", "foo")
        monkeypatch.setenv("PYTHONPATH", "/proj/src")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/lib")
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        assert env["PATH"] == "/opt/x:/usr/bin"
        assert env["VIRTUAL_ENV"] == "/tmp/venv"
        assert env["CONDA_PREFIX"] == "/opt/conda/envs/foo"
        assert env["CONDA_DEFAULT_ENV"] == "foo"
        assert env["PYTHONPATH"] == "/proj/src"
        assert env["LD_LIBRARY_PATH"] == "/opt/lib"

    def test_runtime_keys_absent_when_unset(self, monkeypatch):
        """Optional runtime keys must NOT show up when the parent never set them."""
        for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYTHONPATH"):
            monkeypatch.delenv(key, raising=False)
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYTHONPATH"):
            assert key not in env

    def test_lc_locale_prefix_forwarded(self, monkeypatch):
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        assert env["LC_ALL"] == "en_US.UTF-8"
        assert env["LC_CTYPE"] == "en_US.UTF-8"

    def test_secrets_excluded(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("MIRA_TOKEN", "tok-secret")
        # Even with PATH / VIRTUAL_ENV explicitly forwarded, credential-shaped
        # vars must still be scrubbed by _SENSITIVE_ENV_MARKERS.
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("VIRTUAL_ENV", "/tmp/v")
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        assert "OPENAI_API_KEY" not in env
        assert "MIRA_TOKEN" not in env
        for v in env.values():
            assert "secret" not in v.lower()


class TestBuildEnvWindows:

    _CORE_KEYS = {
        "SYSTEMROOT", "COMSPEC", "USERPROFILE", "HOMEDRIVE",
        "HOMEPATH", "TEMP", "TMP", "PATHEXT", "PATH",
        *_WINDOWS_ENV_KEYS,
    }

    def test_core_keys_always_present(self):
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        # Every core Win32 key must show up, even if defaulted to empty.
        assert self._CORE_KEYS <= set(env)

    def test_optional_python_keys_forwarded(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_ENV", r"C:\proj\.venv")
        monkeypatch.setenv("PYTHONPATH", r"C:\proj\src")
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        assert env["VIRTUAL_ENV"] == r"C:\proj\.venv"
        assert env["PYTHONPATH"] == r"C:\proj\src"

    def test_optional_python_keys_omitted_when_unset(self, monkeypatch):
        for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYTHONPATH"):
            monkeypatch.delenv(key, raising=False)
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        assert "VIRTUAL_ENV" not in env
        assert "CONDA_PREFIX" not in env
        assert "CONDA_DEFAULT_ENV" not in env
        assert "PYTHONPATH" not in env

    def test_secrets_excluded(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("MIRA_TOKEN", "tok-secret")
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        assert "OPENAI_API_KEY" not in env
        assert "MIRA_TOKEN" not in env
        for v in env.values():
            assert "secret" not in v.lower()

    def test_path_has_sensible_default(self):
        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", True),
            patch.dict("os.environ", {}, clear=True),
        ):
            env = ExecTool()._build_env()
        assert "system32" in env["PATH"].lower()

    def test_systemroot_forwarded(self, monkeypatch):
        monkeypatch.setenv("SYSTEMROOT", r"D:\Windows")
        with patch("mira_engine.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        assert env["SYSTEMROOT"] == r"D:\Windows"


# ---------------------------------------------------------------------------
# _spawn
# ---------------------------------------------------------------------------

class TestSpawnUnix:

    @pytest.mark.asyncio
    async def test_uses_bash(self):
        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", False),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = AsyncMock()
            await ExecTool._spawn("echo hi", "/tmp", {"HOME": "/tmp"})

        args = mock_exec.call_args[0]
        assert "bash" in args[0]
        assert "-l" in args
        assert "-c" in args
        assert "echo hi" in args


class TestSpawnWindows:

    @pytest.mark.asyncio
    async def test_uses_comspec_from_env(self):
        env = {"COMSPEC": r"C:\Windows\system32\cmd.exe", "PATH": ""}
        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", True),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = AsyncMock()
            await ExecTool._spawn("dir", r"C:\Users", env)

        args = mock_exec.call_args[0]
        assert "cmd.exe" in args[0]
        assert "/c" in args
        assert "dir" in args

    @pytest.mark.asyncio
    async def test_falls_back_to_default_comspec(self):
        env = {"PATH": ""}
        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", True),
            patch.dict("os.environ", {}, clear=True),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = AsyncMock()
            await ExecTool._spawn("dir", r"C:\Users", env)

        args = mock_exec.call_args[0]
        assert args[0] == "cmd.exe"


# ---------------------------------------------------------------------------
# path_append
# ---------------------------------------------------------------------------

class TestPathAppendPlatform:

    @pytest.mark.asyncio
    async def test_unix_injects_export(self):
        """On Unix, path_append is an export statement prepended to command."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", False),
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(path_append="/opt/bin")
            await tool.execute(command="ls")

        spawned_cmd = mock_spawn.call_args[0][0]
        assert 'export PATH="$PATH:/opt/bin"' in spawned_cmd
        assert spawned_cmd.endswith("ls")

    @pytest.mark.asyncio
    async def test_windows_modifies_env(self):
        """On Windows, path_append is appended to PATH in the env dict."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        captured_env = {}

        async def capture_spawn(cmd, cwd, env):
            captured_env.update(env)
            return mock_proc

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", True),
            patch.object(ExecTool, "_spawn", side_effect=capture_spawn),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(path_append=r"C:\tools\bin")
            await tool.execute(command="dir")

        assert captured_env["PATH"].endswith(r";C:\tools\bin")


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------

class TestSandboxPlatform:

    @pytest.mark.asyncio
    async def test_bwrap_skipped_on_windows(self):
        """bwrap must be silently skipped on Windows, not crash."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", True),
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(sandbox="bwrap")
            result = await tool.execute(command="dir")

        assert "ok" in result
        spawned_cmd = mock_spawn.call_args[0][0]
        assert "bwrap" not in spawned_cmd

    @pytest.mark.asyncio
    async def test_bwrap_applied_on_unix(self):
        """On Unix, sandbox wrapping should still happen normally."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"sandboxed", b"")
        mock_proc.returncode = 0

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", False),
            patch("mira_engine.agent.tools.shell.wrap_command", return_value="bwrap -- sh -c ls") as mock_wrap,
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(sandbox="bwrap", working_dir="/workspace")
            await tool.execute(command="ls")

        mock_wrap.assert_called_once()
        spawned_cmd = mock_spawn.call_args[0][0]
        assert "bwrap" in spawned_cmd


# ---------------------------------------------------------------------------
# end-to-end (mocked subprocess, full execute path)
# ---------------------------------------------------------------------------

class TestExecuteEndToEnd:

    @pytest.mark.asyncio
    async def test_windows_full_path(self):
        """Full execute() flow on Windows: env, spawn, output formatting."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\r\n", b"")
        mock_proc.returncode = 0

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", True),
            patch.object(ExecTool, "_spawn", return_value=mock_proc),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool()
            result = await tool.execute(command="echo hello world")

        assert "hello world" in result
        assert "Exit code: 0" in result

    @pytest.mark.asyncio
    async def test_unix_full_path(self):
        """Full execute() flow on Unix: env, spawn, output formatting."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\n", b"")
        mock_proc.returncode = 0

        with (
            patch("mira_engine.agent.tools.shell._IS_WINDOWS", False),
            patch.object(ExecTool, "_spawn", return_value=mock_proc),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool()
            result = await tool.execute(command="echo hello world")

        assert "hello world" in result
        assert "Exit code: 0" in result

    @pytest.mark.asyncio
    async def test_runtime_context_sets_default_working_dir(self, tmp_path):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0
        instance_workspace = tmp_path / "instance"
        project_dir = tmp_path / "projects" / "alpha"
        instance_workspace.mkdir()
        project_dir.mkdir(parents=True)

        with (
            patch("asyncio.create_subprocess_shell", new_callable=AsyncMock),
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(working_dir=str(instance_workspace))
            tool.set_runtime_context(workspace=project_dir)
            result = await tool.execute(command="pwd")

        assert "ok" in result
        assert mock_spawn.call_args[0][1] == str(project_dir)

    @pytest.mark.asyncio
    async def test_runtime_context_can_be_cleared(self, tmp_path):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0
        instance_workspace = tmp_path / "instance"
        project_dir = tmp_path / "projects" / "alpha"
        instance_workspace.mkdir()
        project_dir.mkdir(parents=True)

        with (
            patch("asyncio.create_subprocess_shell", new_callable=AsyncMock),
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(working_dir=str(instance_workspace))
            tool.set_runtime_context(workspace=project_dir)
            tool.clear_runtime_context()
            result = await tool.execute(command="pwd")

        assert "ok" in result
        assert mock_spawn.call_args[0][1] == str(instance_workspace)
