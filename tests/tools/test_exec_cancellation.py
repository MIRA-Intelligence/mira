from __future__ import annotations

import asyncio
import os
import sys

import pytest

from mira_engine.agent.tools.shell import ExecTool


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_execute_cancellation_kills_process_group(tmp_path) -> None:
    tool = ExecTool(timeout=60, working_dir=str(tmp_path))
    shell_pid = tmp_path / "shell.pid"
    child_pid = tmp_path / "child.pid"
    command = f"echo $$ > {shell_pid}; sleep 60 & echo $! > {child_pid}; wait"
    task = asyncio.create_task(tool.execute(command))
    for _ in range(100):
        if shell_pid.exists() and child_pid.exists():
            break
        await asyncio.sleep(0.02)
    assert shell_pid.exists() and child_pid.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for pid_path in (shell_pid, child_pid):
        pid = int(pid_path.read_text().strip())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
