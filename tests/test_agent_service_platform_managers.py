from types import SimpleNamespace
from unittest.mock import mock_open

from mira_engine.cli.agent_service import (
    EXIT_OK,
    SYSTEMD_UNIT_NAME,
    WINDOWS_SERVICE_NAME,
    AgentPaths,
    SystemdUserServiceManager,
    WindowsServiceManager,
)


def _cp(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_systemd_manager_install_and_status(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        calls.append(cmd)
        if cmd[-2:] == ["is-active", SYSTEMD_UNIT_NAME]:
            return _cp(returncode=0, stdout="active\n")
        return _cp(returncode=0)

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    manager = SystemdUserServiceManager(AgentPaths.default())

    code, _ = manager.install_service()
    assert code == EXIT_OK
    assert manager.paths.systemd_unit.exists()

    status_code, payload = manager.status()
    assert status_code == EXIT_OK
    assert payload["service_mode"] == "systemd-user"
    assert payload["running"] is True
    assert any(cmd[-2:] == ["enable", SYSTEMD_UNIT_NAME] for cmd in calls)


def test_windows_manager_install_and_status(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    popen_calls = []
    running_pids = {4321}

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        calls.append(cmd)
        if cmd[:2] == ["tasklist", "/FI"]:
            pid = int(cmd[2].split()[-1])
            if pid in running_pids:
                return _cp(returncode=0, stdout=f'"mira-engine.exe","{pid}","Console","1","12,000 K"')
            return _cp(returncode=0, stdout="INFO: No tasks are running which match the specified criteria.")
        return _cp(returncode=0)

    fake_proc = SimpleNamespace(pid=4321, poll=lambda: None)
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)

    def fake_popen(*args, **kwargs):  # noqa: ANN001
        popen_calls.append((args, kwargs))
        return fake_proc

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("mira_engine.cli.agent_service.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("builtins.open", mock_open())
    manager = WindowsServiceManager(AgentPaths.default())

    code, _ = manager.install_service()
    assert code == EXIT_OK

    start_code, _ = manager.start()
    assert start_code == EXIT_OK

    status_code, payload = manager.status()
    assert status_code == EXIT_OK
    assert payload["service_mode"] == "windows-background"
    assert payload["running"] is True
    assert payload["windows_pid"] == 4321
    assert popen_calls
    assert popen_calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert popen_calls[0][1]["env"]["PYTHONUNBUFFERED"] == "1"
    assert any(cmd[:2] == ["tasklist", "/FI"] for cmd in calls)
