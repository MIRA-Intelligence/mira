from types import SimpleNamespace

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
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        calls.append(cmd)
        if cmd[1:3] == ["query", WINDOWS_SERVICE_NAME]:
            return _cp(returncode=0, stdout="STATE              : 4  RUNNING")
        return _cp(returncode=0)

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    manager = WindowsServiceManager(AgentPaths.default())

    code, _ = manager.install_service()
    assert code == EXIT_OK

    status_code, payload = manager.status()
    assert status_code == EXIT_OK
    assert payload["service_mode"] == "windows-service"
    assert payload["running"] is True
    assert any(cmd[1:3] == ["create", WINDOWS_SERVICE_NAME] for cmd in calls)
