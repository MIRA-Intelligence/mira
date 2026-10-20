import pytest

from scripts import release_train_smoke


def test_run_reports_success_when_all_checks_pass(monkeypatch):
    responses = {
        "http://127.0.0.1:18790/health": (200, {"status": "ok"}),
        "http://127.0.0.1:18790/version": (200, {"agent_version": "0.1.0", "api_contract": "v1"}),
        "http://127.0.0.1:18790/api/status": (200, {"connected_clients": 0, "channel": "ui"}),
    }
    monkeypatch.setattr(release_train_smoke, "_fetch_json", lambda url, **_: responses[url])

    code, report = release_train_smoke.run("http://127.0.0.1:18790")

    assert code == 0
    assert report["ok"] is True


def test_run_reports_failure_on_missing_contract_fields(monkeypatch):
    responses = {
        "http://127.0.0.1:18790/health": (200, {"status": "ok"}),
        "http://127.0.0.1:18790/version": (200, {"agent_version": "0.1.0"}),
        "http://127.0.0.1:18790/api/status": (500, {}),
    }
    monkeypatch.setattr(release_train_smoke, "_fetch_json", lambda url, **_: responses[url])

    code, report = release_train_smoke.run("http://127.0.0.1:18790")

    assert code == 1
    assert report["ok"] is False


@pytest.mark.parametrize("actual,success", [("0.3.5rc1", True), ("0.3.4", False)])
def test_exact_release_version_and_modular_contract(monkeypatch, actual, success):
    responses = {
        "/health": {"status": "ok"},
        "/version": {"agent_version": actual, "api_contract": "v1"},
        "/api/status": {"connected_clients": 0, "channel": "ui"},
        "/api/workflow/v1/contract": {"module_types": ["initiation", "planning", "experiment", "delivery"]},
    }
    monkeypatch.setattr(release_train_smoke, "_fetch_json", lambda url, **_: (200, responses[url.removeprefix("http://test")]))
    code, report = release_train_smoke.run("http://test", expected_agent="0.3.5rc1", expected_contract="v1")
    assert report["ok"] is success
    assert code == (0 if success else 1)


def test_api_requests_carry_the_gateway_token(monkeypatch):
    seen = {}

    def fake_fetch(url, *, token=None):
        seen[url.removeprefix("http://test")] = token
        return 401, {}

    monkeypatch.setattr(release_train_smoke, "_fetch_json", fake_fetch)
    code, report = release_train_smoke.run("http://test", expected_agent="0.4.0rc1", token="t0k")
    assert seen["/api/status"] == "t0k" and seen["/api/workflow/v1/contract"] == "t0k"
    # The report keeps the HTTP status, so an auth failure is visible.
    assert report["http"]["/api/status"] == 401
    assert code == 1
