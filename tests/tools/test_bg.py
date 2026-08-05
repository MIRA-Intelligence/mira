"""Tests for the background-job registry, ExecTool background path, and BgTool."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mira_engine.agent.tools.bg import (
    BackgroundJobRegistry,
    BgTool,
    spawn_background_job,
)
from mira_engine.agent.tools.shell import ExecTool


def _python_command(*args: str) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([sys.executable, *args])
    return " ".join(shlex.quote(p) for p in (sys.executable, *args))


def _bash_sleep(seconds: float) -> str:
    """Pure-bash sleep that doesn't require Python startup overhead."""
    return f"sleep {seconds}"


@pytest.fixture
def registry() -> BackgroundJobRegistry:
    return BackgroundJobRegistry()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# Registry & raw spawn helpers
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_spawn_background_job_returns_running_handle(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    job = await spawn_background_job(
        registry=registry,
        command=_bash_sleep(2),
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
        description="sleeper",
    )
    try:
        assert job.job_id.startswith("bg-")
        assert job.pid > 0
        assert job.running is True
        assert job in [registry.get(job.job_id)]
        assert len(registry) == 1
        assert job.log_dir.exists()
        assert job.stdout_path.parent == job.log_dir
        assert job.description == "sleeper"
    finally:
        await registry.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_registry_records_exit_code_via_reaper(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    job = await spawn_background_job(
        registry=registry,
        command="exit 7",
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
    )
    # Wait for the reaper to stamp metadata. We don't poll forever — 5s is
    # plenty for `exit 7` even on the slowest CI runner.
    for _ in range(50):
        if not job.running:
            break
        await asyncio.sleep(0.1)
    assert job.running is False
    assert job.exit_code == 7
    assert job.exited_at is not None
    await registry.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_registry_shutdown_kills_live_jobs(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    job = await spawn_background_job(
        registry=registry,
        command=_bash_sleep(60),
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
    )
    assert job.running
    await registry.shutdown()
    # After shutdown the process must be reaped — running flag flips off.
    assert job.running is False
    # Repeat shutdown is idempotent.
    await registry.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_registry_logs_capture_stdout(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    job = await spawn_background_job(
        registry=registry,
        command="echo hello-bg",
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
    )
    for _ in range(50):
        if not job.running:
            break
        await asyncio.sleep(0.1)
    await registry.shutdown()
    text = job.stdout_path.read_text()
    assert "hello-bg" in text


# ---------------------------------------------------------------------------
# ExecTool background path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_exec_tool_background_returns_job_id(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    tool = ExecTool(
        timeout=5,
        working_dir=str(workspace),
        background_registry=registry,
        enable_background=True,
    )
    out = await tool.execute(_bash_sleep(5), background=True, description="train")
    try:
        assert "Started background job bg-" in out
        assert "Logs:" in out
        # Registry now has one job.
        assert len(registry) == 1
        assert next(iter(registry.list())).description == "train"
    finally:
        await registry.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_registry_kills_only_jobs_owned_by_session(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    first = await spawn_background_job(
        registry=registry,
        command=_bash_sleep(60),
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
        owner_session_id="ui:first",
    )
    second = await spawn_background_job(
        registry=registry,
        command=_bash_sleep(60),
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
        owner_session_id="ui:second",
    )
    try:
        assert await registry.kill_by_session("ui:first") == 1
        assert first.running is False
        assert second.running is True
    finally:
        await registry.shutdown()


async def test_exec_tool_background_disabled_returns_helpful_error(
    workspace: Path,
) -> None:
    # No registry, enable_background=False (default) → background=true must
    # bail out with a clear message instead of silently downgrading.
    tool = ExecTool(timeout=5, working_dir=str(workspace))
    out = await tool.execute("echo hi", background=True)
    assert out.startswith("Error: background execution is not enabled")


def test_exec_tool_schema_advertises_background_only_when_enabled(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    plain = ExecTool(timeout=5, working_dir=str(workspace))
    assert "background" not in plain.parameters["properties"]

    bg_enabled = ExecTool(
        timeout=5,
        working_dir=str(workspace),
        background_registry=registry,
        enable_background=True,
    )
    props = bg_enabled.parameters["properties"]
    assert "background" in props
    assert "description" in props
    assert props["background"]["type"] == "boolean"
    # Description should mention the bg companion tool so the LLM knows where
    # to go after a background launch.
    assert "bg" in bg_enabled.description.lower()


# ---------------------------------------------------------------------------
# BgTool — list / status / tail / wait / kill
# ---------------------------------------------------------------------------


async def test_bg_tool_list_empty(registry: BackgroundJobRegistry) -> None:
    tool = BgTool(registry=registry)
    out = await tool.execute(action="list")
    assert "No background jobs" in out


async def test_bg_tool_unknown_action(registry: BackgroundJobRegistry) -> None:
    tool = BgTool(registry=registry)
    out = await tool.execute(action="frobnicate")
    assert out.startswith("Error: unknown action")


async def test_bg_tool_status_unknown_job(registry: BackgroundJobRegistry) -> None:
    tool = BgTool(registry=registry)
    out = await tool.execute(action="status", job_id="bg-deadbeef")
    assert "no background job with id" in out


async def test_bg_tool_status_requires_job_id(registry: BackgroundJobRegistry) -> None:
    tool = BgTool(registry=registry)
    out = await tool.execute(action="status")
    assert "requires job_id" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_bg_tool_full_lifecycle(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    bg = BgTool(registry=registry)
    job = await spawn_background_job(
        registry=registry,
        command="echo lifecycle && sleep 0.2 && echo done",
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
    )

    listing = await bg.execute(action="list")
    assert job.job_id in listing
    assert "running" in listing or "exited" in listing

    status = await bg.execute(action="status", job_id=job.job_id)
    assert job.job_id in status
    assert "command:" in status

    # Wait for completion with a generous timeout.
    waited = await bg.execute(action="wait", job_id=job.job_id, timeout=10)
    assert "exited" in waited
    assert "code=0" in waited

    tail = await bg.execute(action="tail", job_id=job.job_id, tail_lines=20)
    assert "lifecycle" in tail
    assert "done" in tail

    # Killing an already-exited job should be a no-op message, not an error.
    killed = await bg.execute(action="kill", job_id=job.job_id)
    assert "already exited" in killed

    await registry.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_bg_tool_wait_times_out_for_long_job(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    bg = BgTool(registry=registry)
    job = await spawn_background_job(
        registry=registry,
        command=_bash_sleep(30),
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
    )
    out = await bg.execute(action="wait", job_id=job.job_id, timeout=1)
    assert "still running" in out
    assert job.job_id in out
    # Job is still alive; clean up by shutdown.
    assert job.running is True
    await registry.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
async def test_bg_tool_kill_terminates_running_job(
    registry: BackgroundJobRegistry, workspace: Path
) -> None:
    bg = BgTool(registry=registry)
    job = await spawn_background_job(
        registry=registry,
        command=_bash_sleep(60),
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin"},
    )
    assert job.running
    out = await bg.execute(action="kill", job_id=job.job_id)
    assert job.job_id in out
    assert "terminated" in out
    assert job.running is False
    await registry.shutdown()


def test_bg_tool_clamps_wait_timeout(registry: BackgroundJobRegistry) -> None:
    tool = BgTool(registry=registry)
    # Out-of-range values get coerced into the [1, 600] window.
    assert tool._coerce_int(-5, 30, 1, 600) == 1
    assert tool._coerce_int(99999, 30, 1, 600) == 600
    assert tool._coerce_int(None, 30, 1, 600) == 30
    assert tool._coerce_int("not-an-int", 42, 1, 600) == 42
