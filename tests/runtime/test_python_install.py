"""Tests for ``ensure_python_interpreter`` and ``mira runtime install-python``.

The helper short-circuits when the interpreter is already installed and
otherwise shells out to ``uv python install``. We mock ``subprocess.run``
throughout so the tests are hermetic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from mira_engine.runtime.python_env import (
    PythonEnvError,
    UvBinary,
    ensure_project_venv,
    ensure_python_interpreter,
)
from mira_engine.config.schema import PythonRuntimeConfig


def _uv() -> UvBinary:
    return UvBinary(path=Path("/usr/local/bin/uv"), version=(0, 5, 0))


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# ensure_python_interpreter
# ---------------------------------------------------------------------------


class TestEnsurePythonInterpreterInstalled:

    def test_short_circuits_when_already_installed(self) -> None:
        # ``uv python list --only-installed`` reports a 3.11 entry.
        listing = "cpython-3.11.10-linux-x86_64-gnu    /home/me/.uv/python\n"
        with patch("subprocess.run") as run:
            run.return_value = _completed(stdout=listing)
            ensure_python_interpreter(_uv(), "3.11")

        assert run.call_count == 1
        args = run.call_args.args[0]
        assert args[1:4] == ["python", "list", "--only-installed"]

    def test_invokes_install_when_missing(self) -> None:
        # First call: empty listing. Second call: install succeeds.
        with patch("subprocess.run") as run:
            run.side_effect = [_completed(stdout=""), _completed()]
            ensure_python_interpreter(_uv(), "3.11")

        assert run.call_count == 2
        install_args = run.call_args_list[1].args[0]
        assert install_args[1:] == ["python", "install", "3.11"]

    def test_install_failure_raises(self) -> None:
        with patch("subprocess.run") as run:
            run.side_effect = [
                _completed(stdout=""),
                _completed(stderr="boom", returncode=1),
            ]
            with pytest.raises(PythonEnvError, match="install python 3.11"):
                ensure_python_interpreter(_uv(), "3.11")

    def test_handles_full_version_substring(self) -> None:
        # Caller asks for ``3.11.10`` exactly and listing reports the same.
        listing = "cpython-3.11.10-macos-aarch64-none    /Users/me/.uv\n"
        with patch("subprocess.run") as run:
            run.return_value = _completed(stdout=listing)
            ensure_python_interpreter(_uv(), "3.11.10")
        assert run.call_count == 1

    def test_listing_subprocess_error_falls_through_to_install(self) -> None:
        # If ``uv python list`` itself fails (e.g. uv too old), we should
        # still try ``uv python install`` rather than crashing.
        with patch("subprocess.run") as run:
            run.side_effect = [
                OSError("disk error"),
                _completed(),
            ]
            ensure_python_interpreter(_uv(), "3.11")

        assert run.call_count == 2

    def test_listing_nonzero_falls_through_to_install(self) -> None:
        with patch("subprocess.run") as run:
            run.side_effect = [
                _completed(stderr="something", returncode=2),
                _completed(),
            ]
            ensure_python_interpreter(_uv(), "3.11")
        assert run.call_count == 2

    def test_passes_explicit_env_to_subprocess(self) -> None:
        env = {"FOO": "bar"}
        listing = "cpython-3.11.10-linux-x86_64-gnu    /home/me/.uv\n"
        with patch("subprocess.run") as run:
            run.return_value = _completed(stdout=listing)
            ensure_python_interpreter(_uv(), "3.11", env=env)

        actual_env = run.call_args.kwargs.get("env")
        assert actual_env == env


# ---------------------------------------------------------------------------
# Integration with ensure_project_venv
# ---------------------------------------------------------------------------


class TestEnsureProjectVenvInterpreterInstall:

    def test_calls_ensure_python_when_version_pinned(self, tmp_path: Path) -> None:
        cfg = PythonRuntimeConfig(manager="uv", python_version="3.11")
        with patch(
            "mira_engine.runtime.python_env.ensure_python_interpreter"
        ) as ensure, patch(
            "mira_engine.runtime.python_env._create_venv"
        ), patch(
            "mira_engine.runtime.python_env._install_initial_dependencies"
        ):
            ensure_project_venv(tmp_path, cfg, uv=_uv())

        ensure.assert_called_once()
        # First positional arg is the UvBinary, second is version string.
        args, kwargs = ensure.call_args
        assert args[1] == "3.11"

    def test_skips_ensure_python_when_no_version(self, tmp_path: Path) -> None:
        cfg = PythonRuntimeConfig(manager="uv")  # no python_version
        with patch(
            "mira_engine.runtime.python_env.ensure_python_interpreter"
        ) as ensure, patch(
            "mira_engine.runtime.python_env._create_venv"
        ), patch(
            "mira_engine.runtime.python_env._install_initial_dependencies"
        ):
            ensure_project_venv(tmp_path, cfg, uv=_uv())

        ensure.assert_not_called()


# ---------------------------------------------------------------------------
# CLI: mira runtime install-python / info
# ---------------------------------------------------------------------------


class TestCli:
    """``mira runtime install-python`` and ``mira runtime info``.

    We import the typer app lazily inside each test so the tests remain
    fast even when the CLI module pulls in heavy dependencies.
    """

    @staticmethod
    def _runner() -> tuple[CliRunner, object]:
        from mira_engine.cli.commands import app
        return CliRunner(), app

    def test_install_python_invokes_helper_with_explicit_version(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=_uv()
        ), patch(
            "mira_engine.runtime.python_env.ensure_python_interpreter"
        ) as ensure, patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(python=PythonRuntimeConfig(manager="uv"))
                )
            )
            result = runner.invoke(
                app, ["runtime", "install-python", "--version", "3.11"]
            )

        assert result.exit_code == 0, result.stdout
        ensure.assert_called_once()
        assert ensure.call_args.args[1] == "3.11"

    def test_install_python_uses_config_version_when_omitted(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=_uv()
        ), patch(
            "mira_engine.runtime.python_env.ensure_python_interpreter"
        ) as ensure, patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(
                        python=PythonRuntimeConfig(
                            manager="uv", python_version="3.11.10"
                        )
                    )
                )
            )
            result = runner.invoke(app, ["runtime", "install-python"])

        assert result.exit_code == 0, result.stdout
        ensure.assert_called_once()
        assert ensure.call_args.args[1] == "3.11.10"

    def test_install_python_errors_when_no_version_anywhere(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(python=PythonRuntimeConfig(manager="uv"))
                )
            )
            result = runner.invoke(app, ["runtime", "install-python"])

        # Typer/Click normalize non-zero exit codes when stderr is captured
        # alongside stdout; we just care the command failed and printed help.
        assert result.exit_code != 0
        assert "No Python version specified" in result.stdout

    def test_install_python_errors_when_uv_missing(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=None
        ), patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(
                        python=PythonRuntimeConfig(manager="uv", python_version="3.11")
                    )
                )
            )
            result = runner.invoke(app, ["runtime", "install-python"])

        assert result.exit_code == 1
        assert "uv not found" in result.stdout

    def test_install_python_propagates_helper_failure(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=_uv()
        ), patch(
            "mira_engine.runtime.python_env.ensure_python_interpreter",
            side_effect=PythonEnvError("nope"),
        ), patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(
                        python=PythonRuntimeConfig(manager="uv", python_version="3.11")
                    )
                )
            )
            result = runner.invoke(app, ["runtime", "install-python"])

        assert result.exit_code == 1
        assert "nope" in result.stdout

    def test_info_when_disabled(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(python=PythonRuntimeConfig(manager="off"))
                )
            )
            result = runner.invoke(app, ["runtime", "info"])

        assert result.exit_code == 0
        assert "Manager: off" in result.stdout
        assert "disabled" in result.stdout

    def test_info_when_enabled(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=_uv()
        ), patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(
                        python=PythonRuntimeConfig(
                            manager="uv",
                            python_version="3.11",
                            baseline_requirements=["numpy"],
                        )
                    )
                )
            )
            result = runner.invoke(app, ["runtime", "info"])

        assert result.exit_code == 0
        assert "Manager: uv" in result.stdout
        assert "3.11" in result.stdout
        assert "numpy" in result.stdout
        # ``Path`` stringifies with backslashes on Windows, so derive the
        # expected path string from the same Path the CLI will render.
        assert str(_uv().path) in result.stdout

    def test_info_when_uv_missing(self) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=None
        ), patch(
            "mira_engine.cli.commands._load_runtime_config"
        ) as load:
            load.return_value = MagicMock(
                tools=MagicMock(
                    exec=MagicMock(python=PythonRuntimeConfig(manager="uv"))
                )
            )
            result = runner.invoke(app, ["runtime", "info"])

        assert result.exit_code == 0
        assert "not found" in result.stdout
