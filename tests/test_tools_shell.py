from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import subprocess
import sys

from mira_engine.agent.tools.shell import ExecTool


def _python_script_command(script_path: Path) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([sys.executable, str(script_path)])
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"


def _python_script_command(script_path: Path) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([sys.executable, str(script_path)])
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"


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


async def test_execute_returns_output_and_stderr(tmp_path) -> None:
    tool = ExecTool(timeout=5)
    script_path = tmp_path / "emit_stdout_stderr.py"
    script_path.write_text(
        "import sys\nprint('ok')\nprint('err', file=sys.stderr)\n",
        encoding="utf-8",
    )
    output = await tool.execute(_python_script_command(script_path))
    assert "ok" in output
    assert "STDERR:" in output
    assert "err" in output


async def test_execute_reports_nonzero_exit_code(tmp_path) -> None:
    tool = ExecTool(timeout=5)
    script_path = tmp_path / "exit_nonzero.py"
    script_path.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    output = await tool.execute(_python_script_command(script_path))
    assert "Exit code: 3" in output


async def test_execute_timeout_returns_error(tmp_path) -> None:
    tool = ExecTool(timeout=1)
    script_path = tmp_path / "sleep_long.py"
    script_path.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    output = await tool.execute(_python_script_command(script_path))
    assert output == "Error: Command timed out after 1 seconds"


async def test_execute_truncates_long_output(tmp_path) -> None:
    tool = ExecTool(timeout=5)
    script_path = tmp_path / "long_output.py"
    script_path.write_text("print('x' * 11050)\n", encoding="utf-8")
    output = await tool.execute(_python_script_command(script_path))
    assert "truncated" in output


async def test_execute_handles_subprocess_creation_error(monkeypatch) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError("cannot spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom)
    tool = ExecTool(timeout=5)
    output = await tool.execute("echo hi")
    assert output == "Error executing command: cannot spawn"
