import json

from typer.testing import CliRunner

from mira_engine.cli.agent_service import DEFAULT_PORT, _gateway_service_args, app


def test_start_requires_install(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 2
    assert "install-service" in result.stdout


def test_install_start_status_stop_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()

    install = runner.invoke(app, ["install-service"])
    assert install.exit_code == 0

    start = runner.invoke(app, ["start"])
    assert start.exit_code == 0

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert payload["installed"] is True
    assert payload["running"] is True
    assert payload["port"] == DEFAULT_PORT

    stop = runner.invoke(app, ["stop"])
    assert stop.exit_code == 0

    uninstall = runner.invoke(app, ["uninstall-service"])
    assert uninstall.exit_code == 0


def test_doctor_reports_health_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()

    runner.invoke(app, ["install-service"])
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["healthy"] is True
    assert "checks" in payload


def test_gateway_service_args_use_hidden_command_when_frozen(monkeypatch):
    monkeypatch.setattr("mira_engine.cli.agent_service.sys.executable", "/tmp/mira-engine")
    monkeypatch.setattr("mira_engine.cli.agent_service.sys.frozen", True, raising=False)

    args = _gateway_service_args("127.0.0.1", 18790)

    assert args == [
        "/tmp/mira-engine",
        "run-gateway",
        "--host",
        "127.0.0.1",
        "--port",
        "18790",
    ]


def test_run_gateway_repairs_the_service_environment_before_the_gateway_starts(monkeypatch):
    # launchd/systemd/Windows services start with the system PATH; the
    # gateway must see Homebrew/winget directories (where `gh` lives) first.
    # The Windows service also needs APPDATA/LOCALAPPDATA pointed at the user
    # (gh's login lives there) before PATH, whose candidates use LOCALAPPDATA.
    import mira_engine.cli.commands as commands

    order: list[str] = []
    monkeypatch.setattr(
        "mira_engine.cli.agent_service.ensure_user_profile_dirs", lambda *a, **k: order.append("profile") or {}
    )
    monkeypatch.setattr(
        "mira_engine.cli.agent_service.ensure_service_path", lambda *a, **k: order.append("path") or []
    )
    monkeypatch.setattr(commands, "gateway", lambda **kwargs: order.append(f"gateway:{kwargs['port']}"))

    result = CliRunner().invoke(app, ["run-gateway", "--port", "18791"])

    assert result.exit_code == 0, result.output
    assert order == ["profile", "path", "gateway:18791"]
