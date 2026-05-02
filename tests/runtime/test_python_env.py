"""Tests for ``mira_engine.runtime.python_env``.

These tests never actually invoke ``uv`` — every subprocess call is
intercepted via ``monkeypatch`` so the suite runs deterministically on
machines that have no Python toolchain installed beyond stock CPython.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from mira_engine.config.schema import PythonRuntimeConfig
from mira_engine.runtime import python_env
from mira_engine.runtime.python_env import (
    MIN_UV_VERSION,
    PythonEnvError,
    UvBinary,
    detect_uv,
    ensure_project_venv,
    project_venv_path,
    venv_bin_dir,
    venv_exists,
    venv_python_path,
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:

    def test_project_venv_path_relative(self, tmp_path: Path) -> None:
        cfg = PythonRuntimeConfig(manager="uv")
        assert project_venv_path(tmp_path, cfg) == tmp_path / ".venv"

    def test_project_venv_path_absolute(self, tmp_path: Path) -> None:
        absolute = tmp_path / "external" / "venv"
        cfg = PythonRuntimeConfig(manager="uv", venv_dir=str(absolute))
        assert project_venv_path(tmp_path, cfg) == absolute

    def test_venv_bin_dir_unix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(python_env.sys, "platform", "darwin")
        assert venv_bin_dir(tmp_path / ".venv") == tmp_path / ".venv" / "bin"

    def test_venv_bin_dir_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(python_env.sys, "platform", "win32")
        assert venv_bin_dir(tmp_path / ".venv") == tmp_path / ".venv" / "Scripts"

    def test_venv_python_path_unix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(python_env.sys, "platform", "linux")
        assert venv_python_path(tmp_path / ".venv") == tmp_path / ".venv" / "bin" / "python"

    def test_venv_python_path_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(python_env.sys, "platform", "win32")
        assert (
            venv_python_path(tmp_path / ".venv")
            == tmp_path / ".venv" / "Scripts" / "python.exe"
        )

    def test_venv_exists_true_when_python_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(python_env.sys, "platform", "linux")
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").touch()
        assert venv_exists(venv) is True

    def test_venv_exists_false_when_dir_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(python_env.sys, "platform", "linux")
        venv = tmp_path / ".venv"
        venv.mkdir()
        assert venv_exists(venv) is False


# ---------------------------------------------------------------------------
# detect_uv
# ---------------------------------------------------------------------------


def _fake_run_factory(stdout: str = "", stderr: str = "", returncode: int = 0):
    def _fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)

    return _fake_run


class TestDetectUv:

    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(python_env.shutil, "which", lambda *_a, **_k: None)
        assert detect_uv() is None

    def test_returns_binary_when_recent_enough(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = tmp_path / "uv"
        fake.touch()
        monkeypatch.setattr(python_env.shutil, "which", lambda *_a, **_k: str(fake))
        monkeypatch.setattr(
            python_env.subprocess, "run", _fake_run_factory(stdout="uv 0.5.4 (abcd)\n")
        )
        result = detect_uv()
        assert isinstance(result, UvBinary)
        assert result.path == fake
        assert result.version == (0, 5, 4)

    def test_rejects_older_than_min(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
    ) -> None:
        fake = tmp_path / "uv"
        fake.touch()
        monkeypatch.setattr(python_env.shutil, "which", lambda *_a, **_k: str(fake))
        monkeypatch.setattr(
            python_env.subprocess, "run", _fake_run_factory(stdout="uv 0.4.20\n")
        )
        with caplog.at_level("WARNING"):
            assert detect_uv() is None
        assert "require >= " in caplog.text

    def test_uses_stderr_when_stdout_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = tmp_path / "uv"
        fake.touch()
        monkeypatch.setattr(python_env.shutil, "which", lambda *_a, **_k: str(fake))
        monkeypatch.setattr(
            python_env.subprocess,
            "run",
            _fake_run_factory(stdout="", stderr="uv 0.6.1\n"),
        )
        assert detect_uv().version == (0, 6, 1)

    def test_handles_subprocess_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = tmp_path / "uv"
        fake.touch()
        monkeypatch.setattr(python_env.shutil, "which", lambda *_a, **_k: str(fake))
        monkeypatch.setattr(
            python_env.subprocess,
            "run",
            _fake_run_factory(stdout="", stderr="boom", returncode=1),
        )
        assert detect_uv() is None

    def test_handles_oserror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = tmp_path / "uv"
        fake.touch()
        monkeypatch.setattr(python_env.shutil, "which", lambda *_a, **_k: str(fake))

        def _raise(*_a, **_k):
            raise OSError("permission denied")

        monkeypatch.setattr(python_env.subprocess, "run", _raise)
        assert detect_uv() is None

    def test_search_path_argument_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, Any] = {}

        def _which(name: str, **kwargs: Any) -> str | None:
            captured["name"] = name
            captured["path"] = kwargs.get("path")
            return None

        monkeypatch.setattr(python_env.shutil, "which", _which)
        detect_uv(search_path="/opt/embedded")
        assert captured == {"name": "uv", "path": "/opt/embedded"}


# ---------------------------------------------------------------------------
# ensure_project_venv
# ---------------------------------------------------------------------------


class TestEnsureProjectVenvDisabled:

    def test_returns_none_when_manager_off(self, tmp_path: Path) -> None:
        cfg = PythonRuntimeConfig()
        assert ensure_project_venv(tmp_path, cfg) is None

    def test_returns_none_when_manager_system(self, tmp_path: Path) -> None:
        cfg = PythonRuntimeConfig(manager="system")
        assert ensure_project_venv(tmp_path, cfg) is None


class TestEnsureProjectVenvUv:

    def _setup_uv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[UvBinary, list[list[str]], list[dict[str, str]]]:
        """Wire up a fake uv that records every invocation and creates a
        plausible venv on the filesystem so subsequent calls are idempotent.
        """
        uv_path = tmp_path / "uv"
        uv_path.touch()
        binary = UvBinary(path=uv_path, version=(0, 5, 4))
        monkeypatch.setattr(python_env.sys, "platform", "linux")

        invocations: list[list[str]] = []
        env_snapshots: list[dict[str, str]] = []

        def _fake_run(args, **kwargs):
            invocations.append(list(args))
            env_snapshots.append(dict(kwargs.get("env", {})))
            if args[1:2] == ["venv"]:
                venv = Path(args[2])
                (venv / "bin").mkdir(parents=True, exist_ok=True)
                (venv / "bin" / "python").touch()
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(python_env.subprocess, "run", _fake_run)
        return binary, invocations, env_snapshots

    def test_creates_venv_on_first_call(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, _ = self._setup_uv(monkeypatch, tmp_path)
        cfg = PythonRuntimeConfig(manager="uv", python_version="3.11")
        venv = ensure_project_venv(tmp_path, cfg, uv=binary)
        assert venv == (tmp_path / ".venv").resolve()
        assert any(call[1:2] == ["venv"] for call in invocations)

    def test_passes_python_version_and_link_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, _ = self._setup_uv(monkeypatch, tmp_path)
        cfg = PythonRuntimeConfig(manager="uv", python_version="3.12", link_mode="clone")
        ensure_project_venv(tmp_path, cfg, uv=binary)
        venv_call = next(call for call in invocations if call[1:2] == ["venv"])
        assert "--python" in venv_call and "3.12" in venv_call
        assert "--link-mode" in venv_call and "clone" in venv_call

    def test_idempotent_when_venv_already_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, _ = self._setup_uv(monkeypatch, tmp_path)
        cfg = PythonRuntimeConfig(manager="uv")
        # First call creates it.
        ensure_project_venv(tmp_path, cfg, uv=binary)
        invocations.clear()
        # Second call should short-circuit before subprocess.
        ensure_project_venv(tmp_path, cfg, uv=binary)
        assert invocations == []

    def test_runs_uv_sync_when_pyproject_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, _ = self._setup_uv(monkeypatch, tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='p'\nversion='0.0.0'\n")
        cfg = PythonRuntimeConfig(manager="uv")
        ensure_project_venv(tmp_path, cfg, uv=binary)
        sync_calls = [call for call in invocations if call[1:2] == ["sync"]]
        assert len(sync_calls) == 1

    def test_installs_requirements_txt_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, _ = self._setup_uv(monkeypatch, tmp_path)
        (tmp_path / "requirements.txt").write_text("numpy\n")
        cfg = PythonRuntimeConfig(manager="uv")
        ensure_project_venv(tmp_path, cfg, uv=binary)
        pip_install_calls = [
            call
            for call in invocations
            if call[1:4] == ["pip", "install", "-r"]
        ]
        assert len(pip_install_calls) == 1
        assert pip_install_calls[0][-1].endswith("requirements.txt")

    def test_installs_baseline_requirements_when_no_manifest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, _ = self._setup_uv(monkeypatch, tmp_path)
        cfg = PythonRuntimeConfig(
            manager="uv", baseline_requirements=["numpy", "pandas"]
        )
        ensure_project_venv(tmp_path, cfg, uv=binary)
        baseline_calls = [
            call
            for call in invocations
            if call[1:3] == ["pip", "install"] and "-r" not in call
        ]
        assert len(baseline_calls) == 1
        assert "numpy" in baseline_calls[0]
        assert "pandas" in baseline_calls[0]

    def test_skips_install_when_no_manifest_and_no_baseline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, _ = self._setup_uv(monkeypatch, tmp_path)
        cfg = PythonRuntimeConfig(manager="uv")
        ensure_project_venv(tmp_path, cfg, uv=binary)
        pip_calls = [call for call in invocations if "pip" in call]
        assert pip_calls == []

    def test_install_step_sets_virtual_env_in_uv_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, env_snapshots = self._setup_uv(monkeypatch, tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='p'\nversion='0.0.0'\n")
        cfg = PythonRuntimeConfig(manager="uv")
        ensure_project_venv(tmp_path, cfg, uv=binary)
        sync_idx = next(i for i, call in enumerate(invocations) if call[1:2] == ["sync"])
        assert (
            env_snapshots[sync_idx].get("VIRTUAL_ENV")
            == str((tmp_path / ".venv").resolve())
        )

    def test_uv_cache_dir_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, _, env_snapshots = self._setup_uv(monkeypatch, tmp_path)
        cfg = PythonRuntimeConfig(manager="uv", cache_dir=str(tmp_path / "cache"))
        ensure_project_venv(tmp_path, cfg, uv=binary)
        assert all(
            env.get("UV_CACHE_DIR") == str(tmp_path / "cache") for env in env_snapshots
        )

    def test_uv_link_mode_forwarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, _, env_snapshots = self._setup_uv(monkeypatch, tmp_path)
        cfg = PythonRuntimeConfig(manager="uv", link_mode="clone")
        ensure_project_venv(tmp_path, cfg, uv=binary)
        assert all(env.get("UV_LINK_MODE") == "clone" for env in env_snapshots)

    def test_outer_virtual_env_dropped_during_create(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        binary, invocations, env_snapshots = self._setup_uv(monkeypatch, tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", "/tmp/outer/venv")
        cfg = PythonRuntimeConfig(manager="uv")
        ensure_project_venv(tmp_path, cfg, uv=binary)
        venv_idx = next(i for i, call in enumerate(invocations) if call[1:2] == ["venv"])
        assert "VIRTUAL_ENV" not in env_snapshots[venv_idx]

    def test_subprocess_failure_raises_python_env_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        uv_path = tmp_path / "uv"
        uv_path.touch()
        binary = UvBinary(path=uv_path, version=(0, 5, 4))

        def _fail(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="boom"
            )

        monkeypatch.setattr(python_env.subprocess, "run", _fail)
        cfg = PythonRuntimeConfig(manager="uv")
        with pytest.raises(PythonEnvError, match="boom"):
            ensure_project_venv(tmp_path, cfg, uv=binary)


class TestEnsureProjectVenvNoUv:

    def test_raises_when_uv_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(python_env, "detect_uv", lambda *_a, **_k: None)
        cfg = PythonRuntimeConfig(manager="uv")
        with pytest.raises(PythonEnvError, match="uv is required"):
            ensure_project_venv(tmp_path, cfg)

    def test_min_version_referenced_in_error_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(python_env, "detect_uv", lambda *_a, **_k: None)
        cfg = PythonRuntimeConfig(manager="uv")
        with pytest.raises(PythonEnvError) as excinfo:
            ensure_project_venv(tmp_path, cfg)
        assert ".".join(map(str, MIN_UV_VERSION)) in str(excinfo.value)
