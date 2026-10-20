"""Lifecycle regressions; OS commands are simulated, no installed service is touched."""

import json
import os
import plistlib
import socket
import sys
import uuid
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mira_engine.cli import agent_service as svc
from mira_engine.cli import gateway_lifecycle as lifecycle


@pytest.fixture(autouse=True)
def simulated_platform_queries(monkeypatch):
    # These are platform contract tests, even when run on a Windows host.
    # Individual SCM tests install their own fake native API below.
    monkeypatch.delattr(svc.psutil, "win_service_get", raising=False)
    monkeypatch.setattr(svc, "_endpoint_gateway", lambda host, port: None)
    if not hasattr(os, "getuid"):
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)


def cp(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


@pytest.fixture
def mac(tmp_path, monkeypatch):
    manager = svc.LaunchdServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.save_state({**manager._default_state(), "installed": True})
    monkeypatch.setattr(manager, "_ready", lambda: True, raising=False)
    return manager


def test_mac_loaded_without_pid_is_not_running(mac, monkeypatch):
    monkeypatch.setattr(mac, "_run_launchctl", lambda *args: cp(out="state = waiting\nlast exit code = 1"))
    assert mac.status()[1]["running"] is False


def test_mac_repeated_start_does_not_kill_running_gateway(mac, monkeypatch):
    calls = []
    def run(*args):
        calls.append(args)
        return cp(out="state = running\npid = 12345")
    monkeypatch.setattr(mac, "_run_launchctl", run)
    assert mac.start()[0] == 0
    assert all(args[0] == "print" for args in calls)


def test_mac_stop_unloads_keepalive_job(mac, monkeypatch):
    calls = []
    loaded = True
    def run(*args):
        nonlocal loaded
        calls.append(args)
        if args[0] == "bootout":
            loaded = False
        return cp() if loaded or args[0] != "print" else cp(113)
    monkeypatch.setattr(mac, "_run_launchctl", run)
    assert mac.stop()[0] == 0
    assert any(args[0] == "bootout" for args in calls)
    assert not any(args[0] == "stop" for args in calls)
    assert mac.stop()[0] == 0


def test_mac_query_permission_error_is_not_stopped(mac, monkeypatch):
    monkeypatch.setattr(mac, "_run_launchctl", lambda *args: cp(1, err="Operation not permitted"))
    assert mac.status()[0] == svc.EXIT_ERROR
    assert mac.stop()[0] == svc.EXIT_ERROR


def test_restart_does_not_start_after_stop_failure(mac, monkeypatch):
    monkeypatch.setattr(mac, "stop", lambda: (1, "timeout"))
    monkeypatch.setattr(mac, "start", lambda: pytest.fail("must not start"))
    assert mac.restart() == (1, "timeout")


def test_restart_cli_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()
    assert runner.invoke(svc.app, ["install-service", "--home", str(tmp_path)]).exit_code == 0
    assert runner.invoke(svc.app, ["restart", "--home", str(tmp_path)]).exit_code == 0


@pytest.mark.parametrize("output", ["Not running", "StartPending", "StopPending", "Error: previously running"])
def test_windows_status_does_not_match_running_substrings(tmp_path, monkeypatch, output):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.paths.ensure()
    manager._staged_wrapper_path().touch()
    monkeypatch.setattr(manager, "_run_wrapper", lambda command: cp(out=output))
    assert manager._wrapper_status()[1] is False


def test_windows_repeated_start_and_stop_are_noops(tmp_path, monkeypatch):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.save_state({**manager._default_state(), "installed": True})
    monkeypatch.setattr(manager, "_wrapper_status", lambda: (True, True, "Started"))
    monkeypatch.setattr(manager, "_ready", lambda: True, raising=False)
    monkeypatch.setattr(manager, "_run_wrapper", lambda command: pytest.fail(command))
    assert manager.start()[0] == 0
    monkeypatch.setattr(manager, "_wrapper_status", lambda: (True, False, "Stopped"))
    assert manager.stop()[0] == 0


def test_mac_start_after_stop_bootstraps_saved_plist(mac, monkeypatch):
    mac._write_plist("127.0.0.1", 23456, home=None, config_path=None)
    original = mac.paths.launchd_plist.read_bytes()
    calls = []
    def run(*args):
        calls.append(args)
        return cp(113) if args[0] == "print" else cp()
    monkeypatch.setattr(mac, "_run_launchctl", run)
    assert mac.start()[0] == 0
    assert ("bootstrap", mac._domain, str(mac.paths.launchd_plist)) in calls
    assert mac.paths.launchd_plist.read_bytes() == original


def test_mac_stop_timeout_does_not_persist_false_success(mac, monkeypatch):
    state = mac.load_state()
    state["running"] = True
    mac.save_state(state)
    monkeypatch.setattr(mac, "_run_launchctl", lambda *args: cp())
    monkeypatch.setattr(mac, "_wait_for_service_unloaded", lambda: False)
    assert mac.stop()[0] == 1
    assert mac.load_state()["running"] is True


def test_mac_other_config_is_not_stopped(mac, monkeypatch, tmp_path):
    mac._write_plist("127.0.0.1", 23456, home=None, config_path=str(tmp_path / "other.json"))
    calls = []
    monkeypatch.setattr(mac, "_run_launchctl", lambda *args: calls.append(args) or cp())
    assert mac.stop()[0] == 1
    assert all(args[0] == "print" for args in calls)


def test_mac_install_teardown_timeout_preserves_plist(mac, monkeypatch):
    mac._write_plist("127.0.0.1", 23456, home=None, config_path=None)
    original = mac.paths.launchd_plist.read_bytes()
    monkeypatch.setattr(mac, "_teardown_existing_job", lambda: False)
    assert mac.install_service(port=34567)[0] == 1
    assert mac.paths.launchd_plist.read_bytes() == original


def test_mac_explicit_install_config_wins_over_old_metadata(mac, monkeypatch, tmp_path):
    state = mac.load_state()
    state["config_path"] = str(tmp_path / "old.json")
    mac.save_state(state)
    monkeypatch.setattr(mac, "_teardown_existing_job", lambda: True)
    monkeypatch.setattr(mac, "_run_launchctl", lambda *args: cp())
    new_config = str(tmp_path / "new.json")
    assert mac.install_service(config_path=new_config)[0] == 0
    plist = plistlib.loads(mac.paths.launchd_plist.read_bytes())
    assert plist["EnvironmentVariables"]["MIRA_CONFIG_PATH"] == new_config
    assert plist["EnvironmentVariables"]["MIRA_HOME"] == str(mac.paths.root)
    assert mac.load_state()["config_path"] == new_config


@pytest.mark.parametrize("manager_type", [svc.LaunchdServiceManager, svc.WindowsServiceManager])
def test_start_timeout_does_not_claim_success(tmp_path, monkeypatch, manager_type):
    manager = manager_type(svc.AgentPaths.for_home(tmp_path))
    monkeypatch.setattr(svc, "_wait_until", lambda predicate: False)
    assert manager._await_started()[0] == 1
    assert manager.load_state()["running"] is False


@pytest.mark.parametrize("output", ["StopPending", "access denied", "Started"])
def test_windows_stop_waits_and_fails_closed(tmp_path, monkeypatch, output):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.save_state({**manager._default_state(), "installed": True, "running": True})
    statuses = iter([(True, True, "Started"), (True, False, output)])
    monkeypatch.setattr(manager, "_wrapper_status", lambda: next(statuses, (True, False, output)))
    monkeypatch.setattr(manager, "_run_wrapper", lambda command: cp())
    monkeypatch.setattr(manager, "_service_pid", lambda: None)
    monkeypatch.setattr(svc, "_wait_until", lambda predicate: predicate())
    assert manager.stop()[0] == 1
    assert manager.load_state()["running"] is True


def test_windows_stop_waits_for_child_exit(tmp_path, monkeypatch):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    statuses = iter([(True, True, "Started"), (True, False, "Stopped")])
    monkeypatch.setattr(manager, "_wrapper_status", lambda: next(statuses, (True, False, "Stopped")))
    monkeypatch.setattr(manager, "_run_wrapper", lambda command: cp())
    monkeypatch.setattr(manager, "_service_pid", lambda: 12345)
    monkeypatch.setattr(svc, "_process_tree", lambda pid: [SimpleNamespace(is_running=lambda: True, status=lambda: "running")])
    monkeypatch.setattr(svc, "_wait_until", lambda predicate: predicate())
    assert manager.stop()[0] == 1


def test_windows_scm_other_installation_is_not_controlled(tmp_path, monkeypatch):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.save_state({**manager._default_state(), "installed": True})
    service = SimpleNamespace(binpath=lambda: str(tmp_path / "other.exe"))
    monkeypatch.setattr(svc.psutil, "win_service_get", lambda name: service, raising=False)
    monkeypatch.setattr(manager, "_run_wrapper", lambda command: pytest.fail("must not mutate another service"))
    assert manager.start()[0] == 1
    assert manager.stop()[0] == 1
    assert manager.status()[0] == 1


def test_windows_status_error_does_not_erase_installed_state(tmp_path, monkeypatch):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.save_state({**manager._default_state(), "installed": True})
    monkeypatch.setattr(manager, "_wrapper_status", lambda: (False, False, "Access denied"))
    assert manager.status()[0] == 1
    assert manager.load_state()["installed"] is True


def test_reused_pid_is_never_signalled(tmp_path, monkeypatch):
    proc = SimpleNamespace(status=lambda: "running", cmdline=lambda: ["mira", "gateway"], create_time=lambda: 2)
    monkeypatch.setattr(lifecycle.psutil, "Process", lambda pid: proc)
    with pytest.raises(RuntimeError, match="reused"):
        lifecycle.verified_process({"pid": 12345, "create_time": 1}, tmp_path)


def test_legacy_pid_wrong_home_is_rejected(tmp_path, monkeypatch):
    proc = SimpleNamespace(status=lambda: "running", cmdline=lambda: ["mira", "gateway"],
                           environ=lambda: {"MIRA_HOME": str(tmp_path / "other")})
    monkeypatch.setattr(lifecycle.psutil, "Process", lambda pid: proc)
    with pytest.raises(RuntimeError, match="legacy"):
        lifecycle.verified_process({"pid": 12345}, tmp_path)


def test_multiple_gateways_require_selection(tmp_path, monkeypatch):
    manager = svc.LocalServiceManager(svc.AgentPaths.for_home(tmp_path))
    monkeypatch.setattr(lifecycle, "gateway_records", lambda root: [{"port": 1}, {"port": 2}])
    assert lifecycle.gateway_action(manager, "stop")[0] == 1


def test_service_managed_gateway_cannot_be_signalled_directly(tmp_path, monkeypatch):
    manager = svc.LocalServiceManager(svc.AgentPaths.for_home(tmp_path))
    monkeypatch.setattr(lifecycle, "gateway_records", lambda root: [{"port": 1, "launch_mode": "launchd"}])
    assert lifecycle.gateway_action(manager, "stop", 1)[0] == 1


def test_unrelated_healthy_listener_cannot_satisfy_start(monkeypatch):
    proc = SimpleNamespace(children=lambda recursive: [], net_connections=lambda kind: [])
    monkeypatch.setattr(svc.psutil, "Process", lambda pid: proc)
    monkeypatch.setattr(svc.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unowned port"))
    assert svc._gateway_healthy(12345, "127.0.0.1", 1234) is False


def test_cli_restart_is_one_locked_transaction(tmp_path, monkeypatch):
    from mira_engine.utils.locks import interprocess_lock
    manager = svc.LocalServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.install_service()
    monkeypatch.setattr(svc, "_manager_for_home", lambda home: manager)
    def stop():
        with pytest.raises(TimeoutError):
            with interprocess_lock(manager.paths.runtime_dir / "service-lifecycle", timeout=0):
                pytest.fail("restart lock was released before stop")
        return 0, "stopped"
    monkeypatch.setattr(manager, "stop", stop)
    assert svc._lifecycle_command(None, "restart")[0] == 0


@pytest.mark.parametrize("windows,force,expected", [(False, False, 0), (True, False, 1), (True, True, 0)])
def test_standalone_restart_preserves_launch_and_requires_windows_force(tmp_path, monkeypatch, windows, force, expected):
    manager = svc.LocalServiceManager(svc.AgentPaths.for_home(tmp_path))
    record = {"pid": 12345, "port": 23456, "host": "127.0.0.1", "create_time": 1,
              "config_path": str(tmp_path / "custom.json"), "launch_mode": "standalone"}
    command = ["python", "-m", "mira_engine.cli.commands", "gateway", "--workspace", str(tmp_path / "work")]
    alive = [True]
    calls = []
    def terminate(*args):
        alive[0] = False
        calls.append("stop")
    proc = SimpleNamespace(environ=lambda: {"MIRA_HOME": str(manager.paths.root)},
                           cwd=lambda: str(tmp_path), cmdline=lambda: command,
                           children=lambda recursive: [], send_signal=terminate, kill=terminate,
                           is_running=lambda: alive[0], status=lambda: "running")
    monkeypatch.setattr(lifecycle, "gateway_records", lambda root: [record])
    monkeypatch.setattr(lifecycle, "verified_process", lambda record, root: proc)
    monkeypatch.setattr(lifecycle, "IS_WINDOWS", windows)
    for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW"):
        monkeypatch.setattr(lifecycle.subprocess, name, 1, raising=False)
    monkeypatch.setattr(svc, "_wait_until", lambda predicate: predicate())
    monkeypatch.setattr(svc, "_gateway_healthy", lambda *args: True)
    def spawn(args, **kwargs):
        calls.append("start")
        assert args == command
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["env"]["MIRA_HOME"] == str(manager.paths.root)
        assert kwargs["env"]["MIRA_CONFIG_PATH"] == record["config_path"]
        assert "MIRA_SERVICE_MODE" not in kwargs["env"]
        return SimpleNamespace(pid=54321, poll=lambda: None)
    monkeypatch.setattr(lifecycle.subprocess, "Popen", spawn)
    assert lifecycle.gateway_action(manager, "restart", force=force)[0] == expected
    assert calls == (["stop", "start"] if expected == 0 else [])


def test_stop_reports_other_gateway_without_killing_it(mac, monkeypatch):
    monkeypatch.setattr(mac, "_run_launchctl", lambda *args: cp(113))
    monkeypatch.setattr(svc, "_endpoint_gateway", lambda host, port: {
        "endpoint": "http://127.0.0.1:23456", "config_path": "/other/config.json",
    })
    code, message = mac.stop()
    assert code == 1
    assert "another Mira gateway" in message
    assert "/other/config.json" in message
    assert "stop --gateway" in message


@pytest.mark.parametrize("native_state,expected", [("running", True), ("stopped", False), ("start_pending", False), ("stop_pending", False)])
def test_windows_native_scm_state(tmp_path, monkeypatch, native_state, expected):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    service = SimpleNamespace(binpath=lambda: f'"{manager._staged_wrapper_path()}"', status=lambda: native_state)
    monkeypatch.setattr(svc.psutil, "win_service_get", lambda name: service, raising=False)
    assert manager._wrapper_status()[:2] == (True, expected)


def test_windows_background_force_reaches_verified_stop(tmp_path, monkeypatch):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.save_state({**manager._default_state(), "service_mode": "windows-background"})
    monkeypatch.setattr(svc, "_manager_for_home", lambda home: manager)
    calls = []
    monkeypatch.setattr(manager._fallback, "stop", lambda force: calls.append(force) or (0, "ok"))
    monkeypatch.setattr(manager._fallback, "start", lambda: (0, "started"))
    assert svc._lifecycle_command(None, "restart", force=True)[0] == 0
    assert calls == [True]


def test_bundled_gateway_explicitly_enables_ui(monkeypatch):
    calls = []
    monkeypatch.setattr("mira_engine.cli.commands.gateway", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(svc.app, ["run-gateway", "--port", "23456"])
    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 23456, "workspace": None,
                      "verbose": False, "config": None, "no_ui": False}]


def test_windows_stop_missing_service_is_idempotent_with_stale_metadata(tmp_path, monkeypatch):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    manager.save_state({**manager._default_state(), "installed": True, "running": True})
    monkeypatch.setattr(manager, "_wrapper_status", lambda: (False, False, "NonExistent"))
    monkeypatch.setattr(manager, "_run_wrapper", lambda command: pytest.fail(command))
    assert manager.stop()[0] == 0
    assert manager.load_state()["running"] is False


def test_windows_stop_pending_is_waited_without_second_stop(tmp_path, monkeypatch):
    manager = svc.WindowsServiceManager(svc.AgentPaths.for_home(tmp_path))
    states = iter([(True, False, "StopPending"), (True, False, "Stopped")])
    monkeypatch.setattr(manager, "_wrapper_status", lambda: next(states, (True, False, "Stopped")))
    monkeypatch.setattr(manager, "_service_pid", lambda: None)
    monkeypatch.setattr(manager, "_run_wrapper", lambda command: pytest.fail(command))
    assert manager.stop()[0] == 0


def test_health_of_same_pid_with_wrong_config_is_not_ready(tmp_path, monkeypatch):
    from io import BytesIO
    conn = SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=23456))
    proc = SimpleNamespace(children=lambda recursive: [], net_connections=lambda kind: [conn])
    monkeypatch.setattr(svc.psutil, "Process", lambda pid: proc)
    responses = iter([{"status": "ok", "service": "mira-gateway", "running": True},
                      {"config_path": str(tmp_path / "other.json")}])
    def response(*args, **kwargs):
        result = BytesIO(json.dumps(next(responses)).encode())
        result.status = 200
        return result
    monkeypatch.setattr(svc.urllib.request, "urlopen", response)
    assert not svc._gateway_healthy(12345, "127.0.0.1", 23456, str(tmp_path / "expected.json"))


def test_gateway_failsafe_keeps_record_when_process_access_is_denied(tmp_path, monkeypatch):
    import hashlib

    import typer

    from mira_engine.cli.commands import _reserve_gateway_endpoint
    monkeypatch.setenv("MIRA_HOME", str(tmp_path))
    monkeypatch.delenv("MIRA_SKIP_GATEWAY_FAILSAVE", raising=False)
    key = hashlib.sha256(b"127.0.0.1:23456").hexdigest()[:16]
    pid_file = tmp_path / "runtime" / "gateways" / f"{key}.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("12345")
    monkeypatch.setattr(svc.psutil, "pid_exists", lambda pid: True)
    def denied(pid):
        raise svc.psutil.AccessDenied(pid)
    monkeypatch.setattr(svc.psutil, "Process", denied)
    with pytest.raises(typer.Exit):
        _reserve_gateway_endpoint("127.0.0.1", 23456)
    assert pid_file.read_text() == "12345"


def test_gateway_failsafe_ignores_reused_pid_birth_time(tmp_path, monkeypatch):
    import hashlib

    from mira_engine.cli.commands import _reserve_gateway_endpoint
    monkeypatch.setenv("MIRA_HOME", str(tmp_path))
    monkeypatch.delenv("MIRA_SKIP_GATEWAY_FAILSAVE", raising=False)
    key = hashlib.sha256(b"127.0.0.1:23456").hexdigest()[:16]
    pid_file = tmp_path / "runtime" / "gateways" / f"{key}.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("12345")
    discovery = tmp_path / "runtime" / "gateways.json"
    discovery.write_text(json.dumps({"instances": {key: {"pid": 12345, "create_time": 1}}}))
    original_process = svc.psutil.Process
    reused = SimpleNamespace(create_time=lambda: 2, cmdline=lambda: ["mira", "gateway"])
    monkeypatch.setattr(svc.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(svc.psutil, "Process", lambda pid: reused if pid == 12345 else original_process(pid))
    class FreeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, seconds): pass
        def connect_ex(self, address): return 1
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: FreeSocket())
    _reserve_gateway_endpoint("127.0.0.1", 23456)
    assert pid_file.read_text() == str(os.getpid())
    assert json.loads(discovery.read_text())["instances"][key]["create_time"] != 1


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("MIRA_TEST_REAL_LAUNCHD") != "1",
                    reason="opt-in isolated launchd integration")
def test_real_macos_lifecycle(tmp_path, monkeypatch):
    label = f"com.projectmira.lifecycle-test.{uuid.uuid4().hex}"
    monkeypatch.setattr(svc, "LAUNCHD_LABEL", label)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    manager = svc.LaunchdServiceManager(svc.AgentPaths.for_home(tmp_path))
    # A tiny real HTTP worker exercises OS process ownership and health without
    # model credentials, touching neither the user's gateway nor LaunchAgents.
    from pathlib import Path
    monkeypatch.setattr(svc, "_gateway_service_args", lambda host, port: [
        sys.executable, str(Path(__file__).resolve()), "--serve", str(port),
    ])
    try:
        assert manager.install_service(port=port)[0] == 0
        assert manager.start()[0] == 0
        before = manager.status()[1]
        assert before["running"] and before["healthy"]
        assert manager.start()[0] == 0
        assert manager.status()[1]["pid"] == before["pid"]
        assert manager.stop()[0] == 0
        assert not manager.status()[1]["running"]
        assert manager.stop()[0] == 0
        assert manager.start()[0] == 0
        restarted = manager.status()[1]["pid"]
        assert restarted != before["pid"]
        assert manager.restart()[0] == 0
        assert manager.status()[1]["pid"] != restarted
        assert manager.status()[1]["healthy"]
    finally:
        manager._run_launchctl("bootout", manager._service_target)
        assert manager._wait_for_service_unloaded()


if __name__ == "__main__" and "--serve" in sys.argv:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "service": "mira-gateway", "running": True,
                                        "config_path": os.environ.get("MIRA_CONFIG_PATH")}).encode())
    HTTPServer(("127.0.0.1", int(sys.argv[-1])), Handler).serve_forever()
