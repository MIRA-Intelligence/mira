import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from medpilot.utils.env import setup_medpilot_env, auto_activate_env

@pytest.fixture
def mock_workspace(tmp_path):
    return tmp_path

def test_setup_env_conda_exists_no_medpilot_accept(mock_workspace):
    """Test conda creation prompt logic without actually running conda."""
    with patch("shutil.which", return_value="/mock/path/to/conda"):
        with patch("subprocess.run") as mock_run:
            # Mock the 'conda env list' output
            mock_run_result = MagicMock()
            mock_run_result.stdout = "base /mock/path/conda/base\nother_env /mock/path/env/other\n"
            mock_run.return_value = mock_run_result
            
            with patch("typer.confirm", return_value=True) as mock_confirm:
                setup_medpilot_env(mock_workspace)
                
                # Check assertions
                mock_confirm.assert_called_once()
                # subprocess.run should be called twice: once for env list, once to create
                assert mock_run.call_count == 2
                create_call = mock_run.call_args_list[1]
                assert create_call[0][0] == ["conda", "create", "-n", "medpilot", "python=3.11", "pip", "-y"]

def test_setup_env_no_conda_create_venv_accept(mock_workspace):
    """Test standard venv creation logic when conda isn't available."""
    with patch("shutil.which", return_value=None):
        # We also need to mock Path.exists so it thinks venv doesn't exist
        with patch.object(Path, "exists", return_value=False):
            with patch("subprocess.run") as mock_run:
                with patch("typer.confirm", return_value=True) as mock_confirm:
                    setup_medpilot_env(mock_workspace)
                    
                    mock_confirm.assert_called_once()
                    mock_run.assert_called_once()
                    args = mock_run.call_args[0][0]
                    assert args[0] == sys.executable
                    assert args[1:3] == ["-m", "venv"]
                    assert str(mock_workspace / "venv") in args[3]

def test_auto_activate_env_conda(mock_workspace):
    """Test auto environment activation for Conda."""
    mock_env = {"PATH": "/usr/bin:/bin"}
    
    with patch("shutil.which", return_value="/mock/path/to/conda"):
        with patch("subprocess.run") as mock_run:
            # Output matching conda env list
            mock_run_result = MagicMock()
            mock_run_result.stdout = "medpilot /mock/conda/envs/medpilot\n"
            mock_run.return_value = mock_run_result
            
            with patch.dict(os.environ, mock_env, clear=True):
                auto_activate_env(mock_workspace)
                
                # Verify environment gets hijacked correctly
                assert "medpilot" in os.environ.get("CONDA_DEFAULT_ENV", "")
                assert "/mock/conda/envs/medpilot" in os.environ.get("CONDA_PREFIX", "")
                
                # bin path validation based on platform
                expected_bin = os.path.join("/mock/conda/envs/medpilot", "Scripts" if sys.platform == "win32" else "bin")
                assert os.environ["PATH"].startswith(expected_bin)

def test_auto_activate_env_venv(mock_workspace):
    """Test auto environment activation for standard venv."""
    mock_env = {"PATH": "/usr/bin:/bin"}
    
    with patch("shutil.which", return_value=None):
        with patch.object(Path, "exists", return_value=True):
            with patch.dict(os.environ, mock_env, clear=True):
                auto_activate_env(mock_workspace)
                
                assert "venv" in os.environ.get("VIRTUAL_ENV", "")
                expected_bin = str(mock_workspace / "venv" / ("Scripts" if sys.platform == "win32" else "bin"))
                assert os.environ["PATH"].startswith(expected_bin)

