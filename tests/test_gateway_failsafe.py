import os
import socket
import pytest
import psutil
import typer
from unittest.mock import MagicMock, patch
from pathlib import Path
from medpilot.cli.commands import _gateway_failsafe_check

@pytest.fixture
def mock_runtime_dir(tmp_path):
    """模拟 ~/.medpilot/runtime 目录"""
    runtime_dir = tmp_path / ".medpilot" / "runtime"
    runtime_dir.mkdir(parents=True)
    return runtime_dir

def test_gateway_pid_lock_prevents_startup(mock_runtime_dir, monkeypatch):
    """测试：当 PID 文件存在且进程运行时，应触发退出"""
    pid_file = mock_runtime_dir / "gateway.pid"
    current_pid = os.getpid()
    pid_file.write_text(str(current_pid))
    
    # 劫持 Path.expanduser
    monkeypatch.setattr(Path, "expanduser", lambda self: pid_file if "gateway.pid" in str(self) else self)
    monkeypatch.setenv("MEDPILOT_SKIP_GATEWAY_FAILSAVE", "")
    
    with pytest.raises(typer.Exit) as exc:
        _gateway_failsafe_check("127.0.0.1", 9999)
    assert exc.value.exit_code == 1

def test_gateway_port_conflict_prevents_startup(mock_runtime_dir, monkeypatch):
    """测试：当端口已被占用时，应触发退出"""
    pid_file = mock_runtime_dir / "gateway.pid"
    if pid_file.exists():
        pid_file.unlink()

    monkeypatch.setattr(Path, "expanduser", lambda self: pid_file if "gateway.pid" in str(self) else self)
    monkeypatch.setenv("MEDPILOT_SKIP_GATEWAY_FAILSAVE", "")

    # 模拟一个正在监听的端口 (connect_ex 返回 0 表示成功连接，即端口被占用)
    class MockSocket:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, *args): pass
        def connect_ex(self, *args): return 0 # 被占用
        def close(self): pass

    monkeypatch.setattr("socket.socket", MockSocket)
    
    with pytest.raises(typer.Exit) as exc:
        _gateway_failsafe_check("127.0.0.1", 8888)
    assert exc.value.exit_code == 1

def test_gateway_creates_pid_file(mock_runtime_dir, monkeypatch):
    """测试：正常检测通过后应创建 PID 文件"""
    pid_file = mock_runtime_dir / "gateway.pid"
    if pid_file.exists():
        pid_file.unlink()
    
    monkeypatch.setattr(Path, "expanduser", lambda self: pid_file if "gateway.pid" in str(self) else self)
    monkeypatch.setenv("MEDPILOT_SKIP_GATEWAY_FAILSAVE", "")

    # 模拟一个没有被占用的端口 (connect_ex 返回非 0)
    class MockSocket:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, *args): pass
        def connect_ex(self, *args): return 111 # 没被占用
        def close(self): pass

    monkeypatch.setattr("socket.socket", MockSocket)

    _gateway_failsafe_check("127.0.0.1", 7777)
        
    assert pid_file.exists()
    assert pid_file.read_text() == str(os.getpid())
