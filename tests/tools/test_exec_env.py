"""Tests for exec tool environment isolation."""

import sys

import pytest

from mira_engine.agent.tools.shell import ExecTool

_UNIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Unix shell commands")


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_does_not_leak_parent_env(monkeypatch):
    """Env vars from the parent process must not be visible to commands."""
    monkeypatch.setenv("MIRA_SECRET_TOKEN", "super-secret-value")
    tool = ExecTool()
    result = await tool.execute(command="printenv MIRA_SECRET_TOKEN")
    assert "super-secret-value" not in result


@pytest.mark.asyncio
async def test_exec_has_working_path():
    """Basic commands should be available via the login shell's PATH."""
    tool = ExecTool()
    result = await tool.execute(command="echo hello")
    assert "hello" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_path_append():
    """The pathAppend config should be available in the command's PATH."""
    tool = ExecTool(path_append="/opt/custom/bin")
    result = await tool.execute(command="echo $PATH")
    assert "/opt/custom/bin" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_path_append_preserves_system_path():
    """pathAppend must not clobber standard system paths."""
    tool = ExecTool(path_append="/opt/custom/bin")
    result = await tool.execute(command="ls /")
    assert "Exit code: 0" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_propagates_parent_path(monkeypatch):
    """Parent PATH must reach subprocesses (don't rely on bash login files)."""
    import os as _os
    original = _os.environ.get("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("PATH", "/usr/local/sentinel:" + original)
    tool = ExecTool()
    result = await tool.execute(command="echo $PATH")
    assert "/usr/local/sentinel" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_propagates_virtual_env_marker(monkeypatch):
    """VIRTUAL_ENV / CONDA_PREFIX must be forwarded so activated envs survive."""
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/fake-venv")
    monkeypatch.setenv("CONDA_PREFIX", "/tmp/fake-conda")
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "fake-env")
    tool = ExecTool()
    result = await tool.execute(
        command="printenv VIRTUAL_ENV; printenv CONDA_PREFIX; printenv CONDA_DEFAULT_ENV"
    )
    assert "/tmp/fake-venv" in result
    assert "/tmp/fake-conda" in result
    assert "fake-env" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_propagates_lc_locale_vars(monkeypatch):
    """LC_* locale family is forwarded via prefix matcher."""
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
    tool = ExecTool()
    result = await tool.execute(command="printenv LC_ALL; printenv LC_CTYPE")
    assert result.count("en_US.UTF-8") == 2


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_still_scrubs_sensitive_even_with_runtime_keys(monkeypatch):
    """Allowlisted PATH must not bring credential-shaped vars along."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-leak")
    monkeypatch.setenv("DATABASE_PASSWORD", "p@ss")
    tool = ExecTool()
    result = await tool.execute(
        command="printenv OPENAI_API_KEY; printenv DATABASE_PASSWORD; echo done"
    )
    assert "sk-do-not-leak" not in result
    assert "p@ss" not in result
    assert "done" in result


@_UNIX_ONLY
@pytest.mark.asyncio
async def test_exec_propagates_dyld_library_path(monkeypatch):
    """Native library search paths must reach subprocesses."""
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/opt/native/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/native/lib")
    tool = ExecTool()
    result = await tool.execute(
        command="printenv DYLD_LIBRARY_PATH; printenv LD_LIBRARY_PATH"
    )
    assert result.count("/opt/native/lib") >= 1  # at least one platform's var hit
