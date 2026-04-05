from types import SimpleNamespace

from medpilot.cli.agent_service import (
    EXIT_OK,
    SYSTEMD_UNIT_NAME,
    AgentPaths,
    SystemdUserServiceManager,
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

    monkeypatch.setattr("medpilot.cli.agent_service.subprocess.run", fake_run)
    manager = SystemdUserServiceManager(AgentPaths.default())

    code, _ = manager.install_service()
    assert code == EXIT_OK
    assert manager.paths.systemd_unit.exists()

    status_code, payload = manager.status()
    assert status_code == EXIT_OK
    assert payload["service_mode"] == "systemd-user"
    assert payload["running"] is True
    assert any(cmd[-2:] == ["enable", SYSTEMD_UNIT_NAME] for cmd in calls)
