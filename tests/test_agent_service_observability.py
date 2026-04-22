import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from mira_engine.cli.agent_service import app


def test_service_commands_emit_structured_jsonl_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()

    runner.invoke(app, ["install-service"])
    runner.invoke(app, ["start"])
    runner.invoke(app, ["stop"])

    status = runner.invoke(app, ["status"])
    payload = json.loads(status.stdout)
    log_file = Path(payload["log_file"])
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert any(line.get("event") == "install_service" for line in lines)
    assert any(line.get("event") == "start_service" for line in lines)
    assert any(line.get("event") == "stop_service" for line in lines)


def test_doctor_export_writes_diagnostics_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIRA_AGENT_SERVICE_MODE", "local")
    runner = CliRunner()

    runner.invoke(app, ["install-service"])
    result = runner.invoke(app, ["doctor", "--export"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    bundle = Path(payload["diagnostics_bundle"])
    assert bundle.exists()

    with zipfile.ZipFile(bundle, "r") as zf:
        names = set(zf.namelist())
        assert "doctor.json" in names
        assert "agent-service.log.tail" in names
