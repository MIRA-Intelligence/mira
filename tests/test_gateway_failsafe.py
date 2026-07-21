import hashlib
import json
import os
from unittest.mock import MagicMock

import psutil
import pytest
import typer

from mira_engine.cli.commands import (
    _gateway_failsafe_check,
    _prepare_gateway_ui_channel,
    _resolve_gateway_ui_enabled,
    _ui_enabled_in_config_file,
)
from mira_engine.config.schema import Config


def _gateway_pid_file(runtime_dir, host: str, port: int):
    endpoint = f"{host}:{port}"
    key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
    return runtime_dir / "gateways" / f"{key}.pid"


@pytest.fixture
def mock_runtime_dir(tmp_path):
    """模拟 ~/.mira/runtime 目录"""
    runtime_dir = tmp_path / ".mira" / "runtime"
    runtime_dir.mkdir(parents=True)
    return runtime_dir


def test_gateway_pid_lock_prevents_startup(mock_runtime_dir, monkeypatch):
    """测试：当 PID 文件存在且进程运行时，应触发退出"""
    pid_file = _gateway_pid_file(mock_runtime_dir, "127.0.0.1", 9999)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    locked_pid = 123456
    pid_file.write_text(str(locked_pid))

    # 通过 MIRA_HOME 将主目录指向临时目录
    monkeypatch.setenv("MIRA_HOME", str(mock_runtime_dir.parent))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == locked_pid)
    proc = MagicMock()
    proc.cmdline.return_value = ["mira", "gateway"]
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    monkeypatch.setenv("MIRA_SKIP_GATEWAY_FAILSAVE", "")

    with pytest.raises(typer.Exit) as exc:
        _gateway_failsafe_check("127.0.0.1", 9999)
    assert exc.value.exit_code == 1

def test_gateway_port_conflict_prevents_startup(mock_runtime_dir, monkeypatch):
    """测试：当端口已被占用时，应触发退出"""
    pid_file = _gateway_pid_file(mock_runtime_dir, "127.0.0.1", 8888)
    if pid_file.exists():
        pid_file.unlink()

    monkeypatch.setenv("MIRA_HOME", str(mock_runtime_dir.parent))
    monkeypatch.setenv("MIRA_SKIP_GATEWAY_FAILSAVE", "")

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
    pid_file = _gateway_pid_file(mock_runtime_dir, "127.0.0.1", 7777)
    if pid_file.exists():
        pid_file.unlink()

    monkeypatch.setenv("MIRA_HOME", str(mock_runtime_dir.parent))
    monkeypatch.setenv("MIRA_SKIP_GATEWAY_FAILSAVE", "")

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
    discovery = json.loads((mock_runtime_dir / "gateways.json").read_text(encoding="utf-8"))
    assert any(
        item["port"] == 7777 and item["pid"] == os.getpid()
        for item in discovery["instances"].values()
    )


def test_gateway_pid_on_other_port_does_not_block_startup(mock_runtime_dir, monkeypatch):
    first_pid = _gateway_pid_file(mock_runtime_dir, "127.0.0.1", 7001)
    first_pid.parent.mkdir(parents=True, exist_ok=True)
    first_pid.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setenv("MIRA_HOME", str(mock_runtime_dir.parent))
    monkeypatch.setenv("MIRA_SKIP_GATEWAY_FAILSAVE", "")

    class MockSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, *_args):
            return None

        def connect_ex(self, *_args):
            return 111

    monkeypatch.setattr("socket.socket", MockSocket)

    _gateway_failsafe_check("127.0.0.1", 7002)

    assert _gateway_pid_file(mock_runtime_dir, "127.0.0.1", 7002).exists()


# ---------------------------------------------------------------------------
# Gateway UI-channel bootstrap (mira onboard no longer required)
# ---------------------------------------------------------------------------


def test_resolve_gateway_ui_enabled_defaults_on_when_unset():
    """A fresh home (no explicit value) turns the UI channel on."""
    assert _resolve_gateway_ui_enabled(None, no_ui=False) is True


def test_resolve_gateway_ui_enabled_respects_explicit_disable():
    """An explicit channels.ui.enabled=false is honored."""
    assert _resolve_gateway_ui_enabled(False, no_ui=False) is False


def test_resolve_gateway_ui_enabled_no_ui_flag_wins():
    """--no-ui overrides everything, even an explicit enable."""
    assert _resolve_gateway_ui_enabled(True, no_ui=True) is False
    assert _resolve_gateway_ui_enabled(None, no_ui=True) is False


def test_ui_enabled_in_config_file_missing_returns_none(tmp_path):
    assert _ui_enabled_in_config_file(tmp_path / "nope.json") is None
    assert _ui_enabled_in_config_file(None) is None


def test_ui_enabled_in_config_file_reads_explicit_value(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"channels": {"ui": {"enabled": False}}}))
    assert _ui_enabled_in_config_file(path) is False

    path.write_text(json.dumps({"channels": {"ui": {"enabled": True}}}))
    assert _ui_enabled_in_config_file(path) is True


def test_ui_enabled_in_config_file_honors_legacy_web_alias(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"channels": {"web": {"enabled": True}}}))
    assert _ui_enabled_in_config_file(path) is True


def test_ui_enabled_in_config_file_unset_section_returns_none(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"channels": {"ui": {"allowFrom": ["*"]}}}))
    assert _ui_enabled_in_config_file(path) is None


def test_prepare_gateway_ui_channel_bootstraps_fresh_home(tmp_path, monkeypatch):
    """No config.json on disk: UI channel is enabled and a default config is written."""
    monkeypatch.setenv("MIRA_HOME", str(tmp_path))
    # Make sure no cached config path leaks across tests.
    monkeypatch.setattr("mira_engine.config.loader._current_config_path", None, raising=False)

    config = Config()
    assert config.channels.ui.enabled is False  # schema default stays conservative

    _prepare_gateway_ui_channel(config, no_ui=False)

    assert config.channels.ui.enabled is True
    assert config.channels.ui.allow_from == ["*"]

    config_path = tmp_path / "config.json"
    assert config_path.exists()
    written = json.loads(config_path.read_text())
    assert written["channels"]["ui"]["enabled"] is True


def test_prepare_gateway_ui_channel_no_ui_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path))
    monkeypatch.setattr("mira_engine.config.loader._current_config_path", None, raising=False)

    config = Config()
    _prepare_gateway_ui_channel(config, no_ui=True)

    assert config.channels.ui.enabled is False


def test_prepare_gateway_ui_channel_respects_existing_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path))
    monkeypatch.setattr("mira_engine.config.loader._current_config_path", None, raising=False)

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"channels": {"ui": {"enabled": False}}}))

    config = Config()
    _prepare_gateway_ui_channel(config, no_ui=False)

    assert config.channels.ui.enabled is False


def test_prepare_gateway_ui_channel_normalizes_dict_section(tmp_path, monkeypatch):
    """A config loaded from disk keeps channels.ui as a dict; normalize it."""
    from mira_engine.config.loader import load_config
    from mira_engine.config.schema import UiChannelConfig

    monkeypatch.setenv("MIRA_HOME", str(tmp_path))
    monkeypatch.setattr("mira_engine.config.loader._current_config_path", None, raising=False)

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"channels": {"ui": {"allowFrom": []}}}))

    config = load_config(config_path)
    # Sanity: loaded-from-JSON channel sections are raw dicts, not models.
    assert isinstance(config.channels.ui, (dict, UiChannelConfig))

    _prepare_gateway_ui_channel(config, no_ui=False)

    ui = config.channels.ui
    assert isinstance(ui, UiChannelConfig)
    assert ui.enabled is True
    assert ui.allow_from == ["*"]
