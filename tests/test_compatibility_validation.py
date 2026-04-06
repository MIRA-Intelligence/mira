import json

from scripts.validate_compatibility import validate_file


def test_validate_file_passes_for_valid_payload(tmp_path):
    payload = {
        "release_train": "2026.04",
        "ui": "0.1.x",
        "agent": "0.1.x",
        "api_contract": "v1",
        "min_agent_for_ui": "0.1.4",
    }
    target = tmp_path / "compatibility.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert validate_file(target) == 0


def test_validate_file_passes_for_rc_payload(tmp_path):
    payload = {
        "release_train": "2026.04rc1",
        "ui": "0.3.0rc1",
        "agent": "0.2.0rc1",
        "api_contract": "v1",
        "min_agent_for_ui": "0.2.0rc1",
    }
    target = tmp_path / "compatibility.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert validate_file(target) == 0


def test_validate_file_fails_for_invalid_payload(tmp_path):
    payload = {
        "release_train": "2026.4",
        "ui": "0.1.2",
        "agent": "0.1.x",
        "api_contract": "version1",
        "min_agent_for_ui": "0.1",
    }
    target = tmp_path / "compatibility.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert validate_file(target) == 1
