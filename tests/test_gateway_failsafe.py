import os
import socket
import pytest
import psutil
from unittest.mock import MagicMock, patch
from pathlib import Path
from typer.testing import CliRunner
from medpilot.cli.commands import app

runner = CliRunner()

@pytest.fixture
def mock_runtime_dir(tmp_path):
    """模拟 ~/.medpilot/runtime 目录"""
    runtime_dir = tmp_path / ".medpilot" / "runtime"
    runtime_dir.mkdir(parents=True)
    return runtime_dir

def test_gateway_pid_lock_prevents_startup(mock_runtime_dir, monkeypatch):
    """测试：当 PID 文件存在且进程运行时，网关应拒绝启动"""
    monkeypatch.setenv("MEDPILOT_SKIP_GATEWAY_FAILSAVE", "")
    pid_file = mock_runtime_dir / "gateway.pid"
    current_pid = os.getpid()
    pid_file.write_text(str(current_pid))
    
    # 劫持 Path.expanduser
    monkeypatch.setattr(Path, "expanduser", lambda self: pid_file if "gateway.pid" in str(self) else self)
    
    result = runner.invoke(app, ["gateway", "--port", "9999"])
    
    assert result.exit_code != 0
    assert "已经在运行中" in result.stdout

def test_gateway_port_conflict_prevents_startup(mock_runtime_dir, monkeypatch):
    """测试：当端口已被占用时，网关应拒绝启动"""
    monkeypatch.setenv("MEDPILOT_SKIP_GATEWAY_FAILSAVE", "")
    pid_file = mock_runtime_dir / "gateway.pid"
    if pid_file.exists():
        pid_file.unlink()

    monkeypatch.setattr(Path, "expanduser", lambda self: pid_file if "gateway.pid" in str(self) else self)

    # 模拟一个正在监听的端口 (connect_ex 返回 0 表示成功连接，即端口被占用)
    class MockSocket:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, *args): pass
        def connect_ex(self, *args): return 0 # 被占用
        def close(self): pass

    # 全局替换 socket.socket 确保函数内能读到
    monkeypatch.setattr("socket.socket", MockSocket)
    
    result = runner.invoke(app, ["gateway", "--port", "8888"])
    
    assert result.exit_code != 0
    assert "端口 8888 已被占用" in result.stdout

def test_gateway_creates_pid_file(mock_runtime_dir, monkeypatch):
    """测试：正常启动时创建 PID 文件"""
    monkeypatch.setenv("MEDPILOT_SKIP_GATEWAY_FAILSAVE", "")
    pid_file = mock_runtime_dir / "gateway.pid"
    if pid_file.exists():
        pid_file.unlink()
    
    monkeypatch.setattr(Path, "expanduser", lambda self: pid_file if "gateway.pid" in str(self) else self)
    
    # 我们 mock 掉后续流程以防测试卡死
    # 因为 Typer 执行会在 atexit 清理，我们模拟中断
    with patch("medpilot.cli.commands.sync_workspace_templates", side_effect=RuntimeError("stop")):
        result = runner.invoke(app, ["gateway", "--port", "7777"])
        
    assert pid_file.exists()
    assert pid_file.read_text() == str(os.getpid())
