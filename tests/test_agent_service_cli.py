import json

from typer.testing import CliRunner

from mira_engine.cli.agent_service import app


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
