import plistlib
import sys
from types import SimpleNamespace

import pytest

from medpilot.cli.agent_service import (
    EXIT_OK,
    LAUNCHD_LABEL,
    AgentPaths,
    LaunchdServiceManager,
)


def _fake_completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_install_writes_plist_and_bootstraps(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        calls.append(cmd)
        return _fake_completed(returncode=0)

    monkeypatch.setattr("medpilot.cli.agent_service.subprocess.run", fake_run)
    manager = LaunchdServiceManager(AgentPaths.default())

    code, _ = manager.install_service()

    assert code == EXIT_OK
    assert manager.paths.launchd_plist.exists()
    payload = plistlib.loads(manager.paths.launchd_plist.read_bytes())
    assert payload["Label"] == LAUNCHD_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert any(cmd[1] == "bootstrap" for cmd in calls)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_status_includes_launchd_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        if cmd[1] == "print":
            return _fake_completed(returncode=0)
        return _fake_completed(returncode=0)

    monkeypatch.setattr("medpilot.cli.agent_service.subprocess.run", fake_run)
    manager = LaunchdServiceManager(AgentPaths.default())
    manager.install_service()

    code, payload = manager.status()

    assert code == EXIT_OK
    assert payload["service_mode"] == "launchd"
    assert payload["launchd_label"] == LAUNCHD_LABEL
    assert payload["running"] is True
