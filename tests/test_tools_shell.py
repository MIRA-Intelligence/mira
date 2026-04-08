from __future__ import annotations

import asyncio

from medpilot.agent.tools.shell import ExecTool


def test_guard_blocks_dangerous_patterns() -> None:
    tool = ExecTool()
    msg = tool._guard_command("rm -rf /tmp/demo", "/tmp")
    assert msg == "Error: Command blocked by safety guard (dangerous pattern detected)"


def test_guard_enforces_allowlist() -> None:
    tool = ExecTool(allow_patterns=[r"^echo"])
    assert tool._guard_command("echo hi", "/tmp") is None
    assert tool._guard_command("ls", "/tmp") == "Error: Command blocked by safety guard (not in allowlist)"


def test_extract_absolute_paths_supports_posix_and_windows() -> None:
    cmd = "cat /tmp/a.txt && type C:\\Users\\a\\b.txt"
    paths = ExecTool._extract_absolute_paths(cmd)
    assert "/tmp/a.txt" in paths
    assert "C:\\Users\\a\\b.txt" in paths


def test_guard_blocks_outside_workspace_and_traversal() -> None:
    tool = ExecTool(restrict_to_workspace=True)
    cwd = "/tmp/workspace"
    assert "path traversal" in tool._guard_command("cat ../secret.txt", cwd)
    assert "outside working dir" in tool._guard_command("cat /etc/hosts", cwd)
    assert tool._guard_command("cat ./local.txt", cwd) is None


async def test_execute_returns_output_and_stderr() -> None:
    tool = ExecTool(timeout=5)
    output = await tool.execute("python -c \"import sys; print('ok'); print('err', file=sys.stderr)\"")
    assert "ok" in output
    assert "STDERR:" in output
    assert "err" in output


async def test_execute_reports_nonzero_exit_code() -> None:
    tool = ExecTool(timeout=5)
    output = await tool.execute("python -c \"import sys; sys.exit(3)\"")
    assert "Exit code: 3" in output


async def test_execute_timeout_returns_error() -> None:
    tool = ExecTool(timeout=1)
    output = await tool.execute("python -c \"import time; time.sleep(2)\"")
    assert output == "Error: Command timed out after 1 seconds"


async def test_execute_truncates_long_output() -> None:
    tool = ExecTool(timeout=5)
    output = await tool.execute("python -c \"print('x'*11050)\"")
    assert "truncated" in output


async def test_execute_handles_subprocess_creation_error(monkeypatch) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError("cannot spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom)
    tool = ExecTool(timeout=5)
    output = await tool.execute("echo hi")
    assert output == "Error executing command: cannot spawn"
