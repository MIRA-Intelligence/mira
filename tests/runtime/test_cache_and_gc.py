"""Tests for venv discovery + cache prune helpers and CLI subcommands.

The discovery / size helpers operate on real temporary directories
(filesystem behaviour is what we want to validate), while the
subprocess-driven helpers are fully mocked.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from mira_engine.config.schema import PythonRuntimeConfig
from mira_engine.runtime.python_env import (
    PythonEnvError,
    UvBinary,
    find_project_venvs,
    prune_uv_cache,
    remove_venv,
)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def _make_venv(project: Path, name: str = ".venv", size_kb: int = 8) -> Path:
    """Create a directory that looks like a venv on disk."""
    venv = project / name
    bin_dir = venv / ("bin" if os.name != "nt" else "Scripts")
    bin_dir.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    # Write a couple of "package files" totaling roughly size_kb KiB.
    (venv / "lib").mkdir()
    (venv / "lib" / "package.so").write_bytes(b"\x00" * (size_kb * 1024))
    return venv


def _uv() -> UvBinary:
    return UvBinary(path=Path("/usr/local/bin/uv"), version=(0, 5, 0))


# ---------------------------------------------------------------------------
# find_project_venvs
# ---------------------------------------------------------------------------


class TestFindProjectVenvs:

    def test_returns_empty_list_for_missing_root(self, tmp_path: Path) -> None:
        assert find_project_venvs(tmp_path / "does-not-exist") == []

    def test_finds_single_venv(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _make_venv(project)
        # File outside venv to set "project activity" mtime.
        (project / "main.py").write_text("print('hi')\n")

        result = find_project_venvs(tmp_path)
        assert len(result) == 1
        info = result[0]
        assert info.project_dir == project.resolve()
        assert info.size_bytes >= 8 * 1024
        assert info.last_used > 0
        assert info.last_project_activity > 0

    def test_skips_directories_without_pyvenv_cfg(self, tmp_path: Path) -> None:
        # A bare ``.venv`` folder with no marker file should be ignored.
        empty = tmp_path / "proj" / ".venv"
        empty.mkdir(parents=True)
        (empty / "stray").write_text("not a venv\n")
        assert find_project_venvs(tmp_path) == []

    def test_does_not_descend_into_venv(self, tmp_path: Path) -> None:
        # If a nested venv contains its own ``.venv`` dir, we should still
        # only report the outer one.
        project = tmp_path / "proj"
        project.mkdir()
        outer = _make_venv(project)
        inner = outer / ".venv"
        inner.mkdir()
        (inner / "pyvenv.cfg").write_text("home = /usr\n")
        result = find_project_venvs(tmp_path)
        assert len(result) == 1
        assert result[0].venv_path == outer

    def test_respects_max_depth(self, tmp_path: Path) -> None:
        # Create proj at depth 7; default max_depth is 6.
        deep = tmp_path
        for i in range(7):
            deep = deep / f"d{i}"
            deep.mkdir()
        _make_venv(deep)
        result = find_project_venvs(tmp_path, max_depth=6)
        # The venv lives at depth 8; out of bounds.
        assert result == []

        result = find_project_venvs(tmp_path, max_depth=10)
        assert len(result) == 1

    def test_sorts_by_size_desc(self, tmp_path: Path) -> None:
        small = tmp_path / "small"
        big = tmp_path / "big"
        small.mkdir()
        big.mkdir()
        _make_venv(small, size_kb=4)
        _make_venv(big, size_kb=64)
        result = find_project_venvs(tmp_path)
        assert [v.project_dir.name for v in result] == ["big", "small"]

    def test_separates_used_vs_project_activity_mtimes(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        venv = _make_venv(project)
        # Make project files older than venv files.
        old = time.time() - 10 * 86400
        (project / "old.py").write_text("# old\n")
        os.utime(project / "old.py", (old, old))
        # Touch a venv file recently.
        (venv / "lib" / "package.so").write_bytes(b"\x00" * 4096)

        result = find_project_venvs(tmp_path)
        assert len(result) == 1
        info = result[0]
        assert info.last_used >= info.last_project_activity

    def test_respects_custom_venv_dir_name(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _make_venv(project, name=".venv-custom")
        # Default search ignores the non-default name.
        assert find_project_venvs(tmp_path) == []
        # Explicit search finds it.
        result = find_project_venvs(tmp_path, venv_dir_name=".venv-custom")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# prune_uv_cache
# ---------------------------------------------------------------------------


class TestPruneUvCache:

    def test_invokes_uv_cache_prune(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = _completed(stdout="cleared 12 packages")
            output = prune_uv_cache(_uv())
        assert "cleared 12 packages" in output
        args = run.call_args.args[0]
        assert args[1:3] == ["cache", "prune"]
        assert "--dry-run" not in args

    def test_passes_dry_run_flag(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = _completed(stdout="would clear 3")
            prune_uv_cache(_uv(), dry_run=True)
        args = run.call_args.args[0]
        assert args[-1] == "--dry-run"

    def test_propagates_uv_failure(self) -> None:
        with patch("subprocess.run") as run:
            run.return_value = _completed(stderr="locked", returncode=1)
            with pytest.raises(PythonEnvError, match="uv cache prune failed"):
                prune_uv_cache(_uv())

    def test_raises_when_uv_missing(self) -> None:
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=None
        ):
            with pytest.raises(PythonEnvError, match="uv is required"):
                prune_uv_cache()

    def test_sets_cache_dir_env(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as run:
            run.return_value = _completed()
            prune_uv_cache(_uv(), cache_dir=str(tmp_path))
        env = run.call_args.kwargs.get("env") or {}
        assert env.get("UV_CACHE_DIR") == str(tmp_path)

    def test_handles_subprocess_oserror(self) -> None:
        with patch("subprocess.run", side_effect=OSError("disk full")):
            with pytest.raises(PythonEnvError, match="failed to prune"):
                prune_uv_cache(_uv())


# ---------------------------------------------------------------------------
# remove_venv
# ---------------------------------------------------------------------------


class TestRemoveVenv:

    def test_returns_zero_when_path_missing(self, tmp_path: Path) -> None:
        assert remove_venv(tmp_path / "ghost") == 0

    def test_deletes_directory_and_reports_size(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        venv = _make_venv(project, size_kb=16)
        size = remove_venv(venv)
        assert size >= 16 * 1024
        assert not venv.exists()


# ---------------------------------------------------------------------------
# CLI: cache-prune & project-gc
# ---------------------------------------------------------------------------


class TestCli:

    @staticmethod
    def _runner() -> tuple[CliRunner, object]:
        from mira_engine.cli.commands import app
        return CliRunner(), app

    @staticmethod
    def _config_mock(workspace: Path) -> MagicMock:
        return MagicMock(
            workspace_path=workspace,
            tools=MagicMock(
                exec=MagicMock(python=PythonRuntimeConfig(manager="uv"))
            ),
        )

    def test_cache_prune_invokes_helper(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=_uv()
        ), patch(
            "mira_engine.runtime.python_env.prune_uv_cache",
            return_value="freed 2 GiB",
        ) as prune, patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(app, ["runtime", "cache-prune"])

        assert result.exit_code == 0, result.stdout
        prune.assert_called_once()
        # dry_run defaults to False.
        kwargs = prune.call_args.kwargs
        assert kwargs.get("dry_run") is False
        assert "freed 2 GiB" in result.stdout

    def test_cache_prune_dry_run(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=_uv()
        ), patch(
            "mira_engine.runtime.python_env.prune_uv_cache",
            return_value="would free 200 MiB",
        ) as prune, patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(app, ["runtime", "cache-prune", "--dry-run"])

        assert result.exit_code == 0, result.stdout
        assert prune.call_args.kwargs.get("dry_run") is True
        assert "Dry run:" in result.stdout

    def test_cache_prune_errors_when_uv_missing(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.runtime.python_env.detect_uv", return_value=None
        ), patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(app, ["runtime", "cache-prune"])

        assert result.exit_code == 1
        assert "uv not found" in result.stdout

    def test_project_gc_lists_venvs(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        project = tmp_path / "proj"
        project.mkdir()
        _make_venv(project)
        (project / "main.py").write_text("# hello\n")

        with patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(
                app, ["runtime", "project-gc", "--root", str(tmp_path)]
            )

        assert result.exit_code == 0, result.stdout
        assert "proj" in result.stdout
        assert "active" in result.stdout

    def test_project_gc_handles_no_venvs(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        with patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(
                app, ["runtime", "project-gc", "--root", str(tmp_path)]
            )

        assert result.exit_code == 0
        assert "no venvs" in result.stdout

    def test_project_gc_marks_stale(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        project = tmp_path / "proj"
        project.mkdir()
        _make_venv(project)
        old = time.time() - 365 * 86400
        # Backdate every project file (incl. venv) to 1 year ago.
        for p in project.rglob("*"):
            try:
                os.utime(p, (old, old))
            except OSError:
                pass

        with patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(
                app,
                [
                    "runtime",
                    "project-gc",
                    "--root",
                    str(tmp_path),
                    "--stale-days",
                    "30",
                ],
            )

        assert result.exit_code == 0
        assert "stale" in result.stdout

    def test_project_gc_deletes_stale(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        project = tmp_path / "proj"
        project.mkdir()
        venv = _make_venv(project, size_kb=32)
        old = time.time() - 365 * 86400
        for p in project.rglob("*"):
            try:
                os.utime(p, (old, old))
            except OSError:
                pass

        with patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(
                app,
                [
                    "runtime",
                    "project-gc",
                    "--root",
                    str(tmp_path),
                    "--delete-stale",
                ],
            )

        assert result.exit_code == 0, result.stdout
        assert not venv.exists()
        assert "removed" in result.stdout
        assert "Reclaimed" in result.stdout

    def test_project_gc_explicit_delete(self, tmp_path: Path) -> None:
        runner, app = self._runner()
        project = tmp_path / "proj"
        project.mkdir()
        venv = _make_venv(project)

        with patch(
            "mira_engine.cli.commands._load_runtime_config",
            return_value=self._config_mock(tmp_path),
        ):
            result = runner.invoke(
                app,
                [
                    "runtime",
                    "project-gc",
                    "--root",
                    str(tmp_path),
                    "--delete",
                    str(venv),
                ],
            )

        assert result.exit_code == 0, result.stdout
        assert not venv.exists()
