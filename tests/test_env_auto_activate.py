from unittest.mock import MagicMock, patch

import pytest

from mira_engine.utils.env import auto_activate_env


class TestAutoActivateEnvConda:
    """Test conda env auto-activation path."""

    @pytest.fixture
    def tmp_workspace(self, tmp_path):
        return tmp_path / "workspace"

    def test_no_conda_falls_back_to_venv(self, tmp_workspace):
        tmp_workspace.mkdir(parents=True, exist_ok=True)
        venv_dir = tmp_workspace / "venv"
        venv_dir.mkdir()
        with patch("shutil.which", return_value=None):
            auto_activate_env(tmp_workspace)

    def test_conda_not_found_no_crash(self, tmp_workspace):
        """On Windows, conda .bat may not be found; should not crash."""
        with patch("shutil.which", return_value="/usr/bin/conda"):
            with patch(
                "mira_engine.utils.env.subprocess.run",
                side_effect=FileNotFoundError(),
            ):
                auto_activate_env(tmp_workspace)

    def test_windows_uses_shell_for_conda(self, tmp_workspace):
        """On Windows, subprocess.run should use shell=True for conda."""
        mock_result = MagicMock()
        mock_result.stdout = "mira    C:\\anaconda\\envs\\mira\n"
        with patch("shutil.which", return_value="conda"):
            with patch("mira_engine.utils.env.subprocess.run", return_value=mock_result) as mock_run:
                with patch("mira_engine.utils.env.sys") as mock_sys:
                    mock_sys.platform = "win32"
                    auto_activate_env(tmp_workspace)
                    mock_run.assert_called_once()
                    call_kwargs = mock_run.call_args[1]
                    assert call_kwargs.get("shell") is True

    def test_linux_does_not_use_shell_for_conda(self, tmp_workspace):
        """On Linux, subprocess.run should NOT use shell for conda."""
        mock_result = MagicMock()
        mock_result.stdout = "mira    /home/user/.conda/envs/mira\n"
        with patch("shutil.which", return_value="/usr/bin/conda"):
            with patch("mira_engine.utils.env.subprocess.run", return_value=mock_result) as mock_run:
                with patch("mira_engine.utils.env.sys") as mock_sys:
                    mock_sys.platform = "linux"
                    auto_activate_env(tmp_workspace)
                    call_kwargs = mock_run.call_args[1]
                    assert call_kwargs.get("shell") is False

    def test_conda_env_path_added_to_path(self, tmp_workspace):
        mock_result = MagicMock()
        mock_result.stdout = "mira       /home/user/.conda/envs/mira\n"
        env_vars = {"PATH": "/usr/bin"}
        with patch("shutil.which", return_value="/usr/bin/conda"):
            with patch("mira_engine.utils.env.subprocess.run", return_value=mock_result):
                with patch("mira_engine.utils.env.os") as mock_os:
                    mock_os.environ = env_vars
                    mock_os.pathsep = ":"
                    mock_os.path.join = lambda *a: "/".join(a)
                    auto_activate_env(tmp_workspace)
        assert "/home/user/.conda/envs/mira/bin" in env_vars["PATH"]
        assert env_vars.get("CONDA_PREFIX") == "/home/user/.conda/envs/mira"
