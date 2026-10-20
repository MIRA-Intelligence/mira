"""Exec tool isolation: working_dir confinement, sandboxed bg jobs and uv, secret-free envs."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mira_engine.agent.tools.bg import BackgroundJobRegistry
from mira_engine.agent.tools.shell import ExecTool
from mira_engine.config.schema import PythonRuntimeConfig
from mira_engine.runtime import python_env

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / "src").mkdir(parents=True)
    return path.resolve()


@posix_only
@pytest.mark.asyncio
@pytest.mark.parametrize("working_dir", ["/", "..", "../other", "~"])
async def test_restricted_working_dir_must_stay_in_the_workspace(workspace: Path, working_dir: str) -> None:
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
    with patch.object(ExecTool, "_spawn") as spawn:
        result = await tool.execute(command="cat /etc/hostname", working_dir=working_dir)
    assert "working_dir outside the workspace" in result
    spawn.assert_not_called()


@posix_only
@pytest.mark.asyncio
async def test_restricted_working_dir_inside_the_workspace_runs(workspace: Path) -> None:
    tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
    result = await tool.execute(command="pwd", working_dir="src")
    assert str(workspace / "src") in result


@posix_only
@pytest.mark.asyncio
async def test_unrestricted_working_dir_is_unchanged(workspace: Path, tmp_path: Path) -> None:
    tool = ExecTool(working_dir=str(workspace))
    result = await tool.execute(command="pwd", working_dir=str(tmp_path))
    assert str(tmp_path.resolve()) in result


@posix_only
@pytest.mark.asyncio
async def test_sandboxed_background_job_runs_inside_bwrap(workspace: Path) -> None:
    registry = BackgroundJobRegistry()
    tool = ExecTool(working_dir=str(workspace), sandbox="bwrap", background_registry=registry,
                    enable_background=True)
    process = AsyncMock()
    process.pid = 4321
    process.returncode = None
    with (
        patch("mira_engine.agent.tools.shell.sandbox_available", return_value=(True, "")),
        patch("asyncio.create_subprocess_exec", return_value=process) as spawn,
    ):
        result = await tool.execute(command="python train.py", background=True)
    assert "Started background job" in result
    argv = list(spawn.call_args[0])
    assert argv[0] == "bwrap" and argv[-3:] == ["bash", "-c", "python train.py"]
    assert "-l" not in argv[argv.index("--"):]


@posix_only
@pytest.mark.asyncio
async def test_sandbox_spec_includes_runtime_skill_dirs_and_policy_paths(workspace: Path, tmp_path: Path) -> None:
    from mira_engine.security import host_policy

    host_policy.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    host_policy.POLICY_PATH.write_text('[sandbox]\nnetwork = "none"\nread_only_paths = ["/srv/ref"]\n')
    host_policy.reset_cache()
    skills = tmp_path / "skills"
    skills.mkdir()
    tool = ExecTool(working_dir=str(workspace), sandbox="bwrap", sandbox_read_only_paths=["/opt/conda"])
    tool.set_runtime_context(workspace=workspace, extra_allowed_dirs=[skills])
    spec = tool._sandbox_spec()
    assert spec.workspace == workspace
    assert spec.network is False
    assert spec.read_only == (Path("/opt/conda").resolve(), Path("/srv/ref").resolve())
    assert spec.extra_read_only == (skills.resolve(),)


def test_uv_env_leaves_out_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MIRA_GATEWAY_TOKEN", "tok")
    monkeypatch.setenv("SOME_PASSWORD", "pw")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = python_env._build_uv_env(PythonRuntimeConfig(manager="uv"), None)
    assert "OPENAI_API_KEY" not in env and "MIRA_GATEWAY_TOKEN" not in env and "SOME_PASSWORD" not in env
    assert env["PATH"] == "/usr/bin"


def test_uv_commands_go_through_the_wrapper(tmp_path: Path, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(args, **kwargs):
        seen.append(args)

        class Result:
            returncode = 0
            stdout = stderr = ""

        return Result()

    monkeypatch.setattr(python_env.subprocess, "run", fake_run)
    uv = python_env.UvBinary(path=Path("/usr/local/bin/uv"), version=(0, 5, 0))
    (tmp_path / "requirements.txt").write_text("numpy\n")
    python_env.ensure_project_venv(tmp_path, PythonRuntimeConfig(manager="uv"), uv=uv,
                                   wrap=lambda args: ["bwrap", "--", *args])
    assert seen and all(args[:2] == ["bwrap", "--"] for args in seen)
    assert any("venv" in args for args in seen)
    assert any("requirements.txt" in " ".join(args) for args in seen)
