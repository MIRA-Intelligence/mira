from mira_engine.cli.agent_service import run_gateway


def test_run_gateway_keeps_ui_enabled_by_default(monkeypatch):
    captured = {}

    def fake_gateway(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("mira_engine.cli.commands.gateway", fake_gateway)

    run_gateway(host="127.0.0.1", port=18790)

    assert captured["no_ui"] is False
