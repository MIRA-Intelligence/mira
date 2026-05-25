import json
import plistlib
from types import SimpleNamespace
from unittest.mock import mock_open

from mira_engine.cli.agent_service import (
    EXIT_ERROR,
    EXIT_OK,
    LAUNCHD_LABEL,
    SYSTEMD_UNIT_NAME,
    WINDOWS_SERVICE_NAME,
    AgentPaths,
    LaunchdServiceManager,
    SystemdUserServiceManager,
    WindowsBackgroundProcessManager,
    WindowsServiceManager,
)


def _cp(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_systemd_manager_install_and_status(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    calls = []

    def fake_run(cmd, capture_output, text, check, **_kwargs):  # noqa: ANN001
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


def test_launchd_manager_writes_bundle_environment(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(agent_service.os, "getuid", lambda: 501, raising=False)
    engine = tmp_path / "app" / "mira-engine"
    engine.parent.mkdir(parents=True)
    engine.write_text("engine", encoding="utf-8")
    manifest = {"schema": 1, "sha256": "abc123", "uiBundleVersion": "0.4.0-rc.3"}
    (engine.parent / "mira-engine.manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    config_path = tmp_path / ".mira" / "config.json"
    monkeypatch.setattr(agent_service.sys, "executable", str(engine))
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)

    calls = []

    def fake_run(cmd, capture_output, text, check, **_kwargs):  # noqa: ANN001
        calls.append(cmd)
        return _cp(returncode=0)

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    manager = LaunchdServiceManager(AgentPaths.for_home(tmp_path))

    code, message = manager.install_service(
        host="127.0.0.1",
        port=18790,
        home=str(tmp_path),
        config_path=str(config_path),
    )

    assert code == EXIT_OK
    assert "launchd service installed" in message
    payload = plistlib.loads(manager.paths.launchd_plist.read_bytes())
    assert payload["Label"] == LAUNCHD_LABEL
    assert payload["ProgramArguments"] == [
        str(engine),
        "run-gateway",
        "--host",
        "127.0.0.1",
        "--port",
        "18790",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["StandardOutPath"] == str(tmp_path / ".mira" / "logs" / "agent-service.log")
    assert payload["StandardErrorPath"] == str(tmp_path / ".mira" / "logs" / "agent-service.log")
    assert payload["EnvironmentVariables"] == {
        "HOME": str(tmp_path),
        "MIRA_CONFIG_PATH": str(config_path),
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
        "PYTHONUNBUFFERED": "1",
    }
    status_code, status_payload = manager.status()
    assert status_code == EXIT_OK
    assert status_payload["engine_executable"] == str(engine)
    assert status_payload["engine_manifest"] == manifest
    assert status_payload["engine_sha256"] == "abc123"
    assert status_payload["launchd_program"] == str(engine)
    assert ["launchctl", "bootout", "gui/501/com.projectmira.engine"] in calls
    assert ["launchctl", "remove", LAUNCHD_LABEL] in calls
    assert ["launchctl", "bootstrap", "gui/501", str(manager.paths.launchd_plist)] in calls


def test_windows_background_manager_install_and_status(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    calls = []
    popen_calls = []
    running_pids = {4321}

    def fake_run(cmd, capture_output, text, check, **_kwargs):  # noqa: ANN001
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
    manager = WindowsBackgroundProcessManager(AgentPaths.default())

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


def test_windows_service_manager_installs_winsw_service(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    engine = tmp_path / "app" / "mira-engine.exe"
    wrapper = engine.with_name("MiraEngineService.exe")
    wrapper.parent.mkdir(parents=True)
    engine.write_text("engine", encoding="utf-8")
    wrapper.write_text("winsw", encoding="utf-8")
    monkeypatch.setattr(agent_service.sys, "executable", str(engine))
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)

    calls = []

    def fake_run(cmd, capture_output, text, check, **_kwargs):  # noqa: ANN001
        calls.append(cmd)
        command = cmd[-1]
        if command == "status":
            return _cp(returncode=0, stdout="Started")
        return _cp(returncode=0)

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    manager = WindowsServiceManager(AgentPaths.default())

    code, message = manager.install_service(
        host="127.0.0.1",
        port=18790,
        home=str(tmp_path),
    )

    assert code == EXIT_OK
    assert "Windows service installed" in message
    staged_wrapper = tmp_path / ".mira" / "runtime" / "MiraEngineService.exe"
    service_xml = tmp_path / ".mira" / "runtime" / "MiraEngineService.xml"
    assert staged_wrapper.is_file()
    xml = service_xml.read_text(encoding="utf-8")
    assert f"<id>{WINDOWS_SERVICE_NAME}</id>" in xml
    assert "run-gateway --host 127.0.0.1 --port 18790" in xml
    assert f'name="USERPROFILE" value="{tmp_path}"' in xml

    start_code, _ = manager.start()
    assert start_code == EXIT_OK

    status_code, payload = manager.status()
    assert status_code == EXIT_OK
    assert payload["service_mode"] == "windows-service"
    assert payload["installed"] is True
    assert payload["running"] is True
    assert payload["windows_service"] == WINDOWS_SERVICE_NAME
    assert any(cmd[-1] == "install" for cmd in calls)
    assert any(cmd[-1] == "start" for cmd in calls)


def test_windows_service_manager_requires_winsw_service_by_default(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MIRA_ENGINE_WINDOWS_BACKGROUND_FALLBACK", raising=False)
    engine = tmp_path / "app" / "mira-engine.exe"
    engine.parent.mkdir(parents=True)
    engine.write_text("engine", encoding="utf-8")
    monkeypatch.setattr(agent_service.sys, "executable", str(engine))
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)
    manager = WindowsServiceManager(AgentPaths.default())

    code, message = manager.install_service(
        host="127.0.0.1",
        port=18790,
        home=str(tmp_path),
    )

    assert code == EXIT_ERROR
    assert "MiraEngineService.exe not found" in message
    assert "requires a real Windows service" in message


def test_windows_service_manager_background_fallback_is_explicit_opt_in(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MIRA_ENGINE_WINDOWS_BACKGROUND_FALLBACK", "1")
    engine = tmp_path / "app" / "mira-engine.exe"
    engine.parent.mkdir(parents=True)
    engine.write_text("engine", encoding="utf-8")
    monkeypatch.setattr(agent_service.sys, "executable", str(engine))
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)
    manager = WindowsServiceManager(AgentPaths.default())

    code, message = manager.install_service(
        host="127.0.0.1",
        port=18790,
        home=str(tmp_path),
    )

    assert code == EXIT_OK
    assert "fallback" in message
    status_code, payload = manager.status()
    assert status_code == EXIT_OK
    assert payload["service_mode"] == "windows-background"


def test_windows_service_manager_stops_existing_wrapper_before_restaging(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    engine = tmp_path / "app" / "mira-engine.exe"
    wrapper = engine.with_name("MiraEngineService.exe")
    staged_wrapper = tmp_path / ".mira" / "runtime" / "MiraEngineService.exe"
    wrapper.parent.mkdir(parents=True)
    staged_wrapper.parent.mkdir(parents=True)
    engine.write_text("engine", encoding="utf-8")
    wrapper.write_text("new winsw", encoding="utf-8")
    staged_wrapper.write_text("old winsw", encoding="utf-8")
    monkeypatch.setattr(agent_service.sys, "executable", str(engine))
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)

    events = []

    def fake_run(cmd, capture_output, text, check, **_kwargs):  # noqa: ANN001
        events.append(cmd[-1])
        return _cp(returncode=0)

    def fake_copy2(source, target, *_args, **_kwargs):  # noqa: ANN001
        events.append("copy")
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    monkeypatch.setattr(agent_service.shutil, "copy2", fake_copy2)
    manager = WindowsServiceManager(AgentPaths.default())

    code, message = manager.install_service(
        host="127.0.0.1",
        port=18790,
        home=str(tmp_path),
    )

    assert code == EXIT_OK
    assert "Windows service installed" in message
    assert events.index("stop") < events.index("copy")
    assert events.index("uninstall") < events.index("copy")
    assert events.index("copy") < events.index("install")
    assert staged_wrapper.read_text(encoding="utf-8") == "new winsw"


def test_windows_service_manager_retries_locked_wrapper_stage(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    engine = tmp_path / "app" / "mira-engine.exe"
    wrapper = engine.with_name("MiraEngineService.exe")
    staged_wrapper = tmp_path / ".mira" / "runtime" / "MiraEngineService.exe"
    wrapper.parent.mkdir(parents=True)
    staged_wrapper.parent.mkdir(parents=True)
    engine.write_text("engine", encoding="utf-8")
    wrapper.write_text("new winsw", encoding="utf-8")
    staged_wrapper.write_text("old winsw", encoding="utf-8")
    monkeypatch.setattr(agent_service.sys, "executable", str(engine))
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(agent_service.time, "sleep", lambda *_args, **_kwargs: None)

    events = []
    copy_attempts = 0

    def fake_run(cmd, capture_output, text, check, **_kwargs):  # noqa: ANN001
        events.append(cmd[-1])
        return _cp(returncode=0)

    def fake_copy2(source, target, *_args, **_kwargs):  # noqa: ANN001
        nonlocal copy_attempts
        copy_attempts += 1
        events.append(f"copy-{copy_attempts}")
        if copy_attempts == 1:
            raise PermissionError("wrapper is locked")
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    monkeypatch.setattr(agent_service.shutil, "copy2", fake_copy2)
    manager = WindowsServiceManager(AgentPaths.default())

    code, message = manager.install_service(
        host="127.0.0.1",
        port=18790,
        home=str(tmp_path),
    )

    assert code == EXIT_OK
    assert "Windows service installed" in message
    assert copy_attempts == 2
    assert events.index("copy-1") < events.index("copy-2")
    assert events.index("uninstall") < events.index("copy-2")
    assert staged_wrapper.read_text(encoding="utf-8") == "new winsw"


def test_windows_service_manager_reports_locked_wrapper_stage_failure(monkeypatch, tmp_path):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    engine = tmp_path / "app" / "mira-engine.exe"
    wrapper = engine.with_name("MiraEngineService.exe")
    staged_wrapper = tmp_path / ".mira" / "runtime" / "MiraEngineService.exe"
    wrapper.parent.mkdir(parents=True)
    staged_wrapper.parent.mkdir(parents=True)
    engine.write_text("engine", encoding="utf-8")
    wrapper.write_text("new winsw", encoding="utf-8")
    staged_wrapper.write_text("old winsw", encoding="utf-8")
    monkeypatch.setattr(agent_service.sys, "executable", str(engine))
    monkeypatch.setattr(agent_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(agent_service.time, "sleep", lambda *_args, **_kwargs: None)

    def fake_run(cmd, capture_output, text, check, **_kwargs):  # noqa: ANN001
        return _cp(returncode=0)

    def fake_copy2(source, target, *_args, **_kwargs):  # noqa: ANN001, ARG001
        raise PermissionError("wrapper is locked")

    monkeypatch.setattr("mira_engine.cli.agent_service.subprocess.run", fake_run)
    monkeypatch.setattr(agent_service.shutil, "copy2", fake_copy2)
    manager = WindowsServiceManager(AgentPaths.default())

    code, message = manager.install_service(
        host="127.0.0.1",
        port=18790,
        home=str(tmp_path),
    )

    assert code == EXIT_ERROR
    assert "failed to stage MiraEngineService.exe" in message
    assert "wrapper is locked" in message
