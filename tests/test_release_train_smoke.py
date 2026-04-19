from scripts import release_train_smoke


def test_run_reports_success_when_all_checks_pass(monkeypatch):
    responses = {
        "http://127.0.0.1:18790/health": (200, {"status": "ok"}),
        "http://127.0.0.1:18790/version": (200, {"agent_version": "0.1.0", "api_contract": "v1"}),
        "http://127.0.0.1:18790/api/status": (200, {"connected_clients": 0, "channel": "web"}),
    }
    monkeypatch.setattr(release_train_smoke, "_fetch_json", lambda url: responses[url])

    code, report = release_train_smoke.run("http://127.0.0.1:18790")

    assert code == 0
    assert report["ok"] is True


def test_run_reports_failure_on_missing_contract_fields(monkeypatch):
    responses = {
        "http://127.0.0.1:18790/health": (200, {"status": "ok"}),
        "http://127.0.0.1:18790/version": (200, {"agent_version": "0.1.0"}),
        "http://127.0.0.1:18790/api/status": (500, {}),
    }
    monkeypatch.setattr(release_train_smoke, "_fetch_json", lambda url: responses[url])

    code, report = release_train_smoke.run("http://127.0.0.1:18790")

    assert code == 1
    assert report["ok"] is False
