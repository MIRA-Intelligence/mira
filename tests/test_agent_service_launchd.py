import plistlib
import sys
from types import SimpleNamespace

import pytest

from mira_engine.cli.agent_service import (
    EXIT_OK,
    LAUNCHD_LABEL,
    AgentPaths,
    LaunchdServiceManager,
)


def _fake_completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _make_install_fake_run(calls, *, bootstrap_returncode=0, bootstrap_stderr=""):
    """Build a `subprocess.run` stub for install_service tests.

    The teardown step now polls ``launchctl print`` until the label leaves the
    domain. Return non-zero for ``print`` so the wait exits immediately rather
    than blocking the test for 15s. Other subcommands default to success.
    """

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        calls.append(cmd)
        subcommand = cmd[1] if len(cmd) > 1 else ""
        if subcommand == "print":
            # Non-zero means "service not registered" — i.e. teardown is done.
            return _fake_completed(returncode=113)
        if subcommand == "bootstrap":
            return _fake_completed(returncode=bootstrap_returncode, stderr=bootstrap_stderr)
        return _fake_completed(returncode=0)

    return fake_run


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_install_writes_plist_and_bootstraps(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    monkeypatch.setattr(
        "mira_engine.cli.agent_service.subprocess.run",
        _make_install_fake_run(calls),
    )
    manager = LaunchdServiceManager(AgentPaths.default())

    code, _ = manager.install_service()

    assert code == EXIT_OK
    assert manager.paths.launchd_plist.exists()
    payload = plistlib.loads(manager.paths.launchd_plist.read_bytes())
    assert payload["Label"] == LAUNCHD_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert any(cmd[1] == "bootout" for cmd in calls)
    assert any(cmd[1] == "remove" for cmd in calls)
    assert any(cmd[1] == "bootstrap" for cmd in calls)
    # Teardown must wait for the label to leave the domain before bootstrap
    # (otherwise `launchctl bootstrap` returns "Bootstrap failed: 5").
    assert any(cmd[1] == "print" for cmd in calls)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_install_does_not_update_state_when_bootstrap_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    monkeypatch.setattr(
        "mira_engine.cli.agent_service.subprocess.run",
        _make_install_fake_run(
            calls,
            bootstrap_returncode=5,
            bootstrap_stderr="Bootstrap failed: 5: Input/output error",
        ),
    )
    manager = LaunchdServiceManager(AgentPaths.default())
    manager.save_state({
        **manager._default_state(),
        "installed": True,
        "engine_sha256": "old-sha",
    })

    code, message = manager.install_service()

    assert code != EXIT_OK
    assert "Bootstrap failed: 5" in message
    assert manager.load_state()["engine_sha256"] == "old-sha"


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_install_restores_previous_plist_when_bootstrap_fails(monkeypatch, tmp_path):
    """Failed installs must not leave a half-written plist on disk."""
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    monkeypatch.setattr(
        "mira_engine.cli.agent_service.subprocess.run",
        _make_install_fake_run(
            calls,
            bootstrap_returncode=5,
            bootstrap_stderr="Bootstrap failed: 5: Input/output error",
        ),
    )
    manager = LaunchdServiceManager(AgentPaths.default())
    manager.paths.launchd_plist.parent.mkdir(parents=True, exist_ok=True)
    original_plist = b"<previous-plist-bytes/>"
    manager.paths.launchd_plist.write_bytes(original_plist)

    code, _ = manager.install_service()

    assert code != EXIT_OK
    assert manager.paths.launchd_plist.read_bytes() == original_plist


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_install_removes_plist_when_bootstrap_fails_and_no_previous(monkeypatch, tmp_path):
    """Failed first-time installs should not leave dangling plist behind."""
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    monkeypatch.setattr(
        "mira_engine.cli.agent_service.subprocess.run",
        _make_install_fake_run(calls, bootstrap_returncode=5, bootstrap_stderr="boom"),
    )
    manager = LaunchdServiceManager(AgentPaths.default())

    code, _ = manager.install_service()

    assert code != EXIT_OK
    assert not manager.paths.launchd_plist.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_teardown_waits_for_launchd_to_release_label(monkeypatch, tmp_path):
    """When the old engine is still draining clients, bootout returns before
    launchd has actually released the label. We must keep polling
    ``launchctl print`` until the label is gone so the subsequent bootstrap
    does not race and return "Bootstrap failed: 5"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("mira_engine.cli.agent_service.time.sleep", lambda _s: None)

    print_calls = {"count": 0}
    bootstrap_attempts = {"count": 0}

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        subcommand = cmd[1] if len(cmd) > 1 else ""
        if subcommand == "print":
            print_calls["count"] += 1
            # Simulate the old engine taking 4 polls (~1s of wall time
            # would be needed without the time.sleep monkeypatch) before
            # launchd reports the label as gone.
            if print_calls["count"] < 4:
                return _fake_completed(returncode=0)  # still loaded
            return _fake_completed(returncode=113)  # finally unloaded
        if subcommand == "bootstrap":
            bootstrap_attempts["count"] += 1
            # Bootstrap only succeeds once teardown has actually released
            # the label, i.e. after at least one print loop reported it gone.
            if print_calls["count"] < 4:
                return _fake_completed(returncode=5, stderr="Bootstrap failed: 5")
            return _fake_completed(returncode=0)
        return _fake_completed(returncode=0)

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    manager = LaunchdServiceManager(AgentPaths.default())

    code, _ = manager.install_service()

    assert code == EXIT_OK
    assert print_calls["count"] >= 4
    assert bootstrap_attempts["count"] >= 1


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_teardown_wait_bounded_by_timeout(monkeypatch, tmp_path):
    """If launchd never reports the label gone we still bail rather than
    hanging the install forever."""
    monkeypatch.setenv("HOME", str(tmp_path))

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("mira_engine.cli.agent_service.time.sleep", fake_sleep)

    fake_now = {"value": 0.0}

    def fake_monotonic() -> float:
        fake_now["value"] += 0.25
        return fake_now["value"]

    monkeypatch.setattr("mira_engine.cli.agent_service.time.monotonic", fake_monotonic)

    manager = LaunchdServiceManager(AgentPaths.default())
    monkeypatch.setattr(
        manager,
        "_run_launchctl",
        lambda *args: _fake_completed(returncode=0),  # label never goes away
    )

    assert manager._wait_for_service_unloaded(timeout_s=2.0) is False
    # Polling actually slept between attempts rather than busy-looping.
    assert sleeps and all(s == 0.25 for s in sleeps)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_uninstall_removes_label_from_cache(monkeypatch, tmp_path):
    """Uninstall must call `launchctl remove` (in addition to bootout) so the
    label does not stick around in launchd's cache and confuse a later
    reinstall."""
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        "mira_engine.cli.agent_service.subprocess.run",
        _make_install_fake_run(calls),
    )
    manager = LaunchdServiceManager(AgentPaths.default())
    manager.paths.launchd_plist.parent.mkdir(parents=True, exist_ok=True)
    manager.paths.launchd_plist.write_bytes(b"<placeholder/>")
    manager.save_state({**manager._default_state(), "installed": True})

    manager.uninstall_service()

    assert any(cmd[1] == "bootout" for cmd in calls)
    assert any(cmd[1] == "remove" and cmd[2] == LAUNCHD_LABEL for cmd in calls)
    assert not manager.paths.launchd_plist.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd tests are macOS-specific")
def test_launchd_status_includes_launchd_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    install_done = {"flag": False}

    def fake_run(cmd, capture_output, text, check):  # noqa: ANN001
        subcommand = cmd[1] if len(cmd) > 1 else ""
        if subcommand == "print":
            # During install teardown we want to report "label unloaded" so the
            # wait exits immediately. After install, `status()` queries print
            # and expects success (label loaded) to report running=True.
            return _fake_completed(returncode=0 if install_done["flag"] else 113)
        return _fake_completed(returncode=0)

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    manager = LaunchdServiceManager(AgentPaths.default())
    manager.install_service()
    install_done["flag"] = True

    code, payload = manager.status()

    assert code == EXIT_OK
    assert payload["service_mode"] == "launchd"
    assert payload["launchd_label"] == LAUNCHD_LABEL
    assert payload["running"] is True
