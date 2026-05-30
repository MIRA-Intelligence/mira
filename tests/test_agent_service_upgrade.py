import json

from typer.testing import CliRunner

from mira_engine.cli import agent_service as agent_service_mod


def test_upgrade_success_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()

    runner.invoke(agent_service_mod.app, ["install-service"])

    versions = iter(["0.1.0", "0.2.0"])
    monkeypatch.setattr(agent_service_mod, "_current_version", lambda _package: next(versions, "0.2.0"))
    monkeypatch.setattr(agent_service_mod, "_pip_upgrade", lambda _spec: (0, "ok"))
    monkeypatch.setattr(agent_service_mod, "_health_check", lambda _port: True)

    result = runner.invoke(agent_service_mod.app, ["upgrade", "--package", "mira"])

    assert result.exit_code == 0
    assert "Upgrade successful" in result.stdout

    status = runner.invoke(agent_service_mod.app, ["status"])
    payload = json.loads(status.stdout)
    assert payload["running"] is True


def test_upgrade_failure_rolls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()

    runner.invoke(agent_service_mod.app, ["install-service"])

    calls: list[str] = []

    def fake_pip_upgrade(spec: str):
        calls.append(spec)
        if len(calls) == 1:
            return (1, "upgrade failed")
        return (0, "rollback ok")

    monkeypatch.setattr(agent_service_mod, "_current_version", lambda _package: "0.1.0")
    monkeypatch.setattr(agent_service_mod, "_pip_upgrade", fake_pip_upgrade)
    monkeypatch.setattr(agent_service_mod, "_health_check", lambda _port: True)

    result = runner.invoke(agent_service_mod.app, ["upgrade", "--package", "mira"])

    assert result.exit_code == 1
    assert "Rolled back package" in result.stdout
    assert calls[0] == "mira"
    assert calls[1] == "mira==0.1.0"
