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


def _write_payload(tmp_path, agent: str) -> object:
    payload = {
        "release_train": "2026.04rc1",
        "ui": "0.3.0rc1",
        "agent": agent,
        "api_contract": "v1",
        "min_agent_for_ui": "0.2.0rc1",
    }
    target = tmp_path / "compatibility.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_require_agent_passes_when_pin_matches_tag_exactly(tmp_path):
    target = _write_payload(tmp_path, agent="0.2.0rc4")
    assert validate_file(target, require_agent="0.2.0rc4") == 0


def test_require_agent_fails_when_pin_does_not_match_tag(tmp_path):
    target = _write_payload(tmp_path, agent="0.2.0rc3")
    assert validate_file(target, require_agent="0.2.0rc4") == 1


def test_require_agent_passes_when_minor_range_covers_tag(tmp_path):
    target = _write_payload(tmp_path, agent="0.2.x")
    assert validate_file(target, require_agent="0.2.5") == 0
    assert validate_file(target, require_agent="0.2.0rc7") == 0


def test_require_agent_fails_when_minor_range_does_not_cover_tag(tmp_path):
    target = _write_payload(tmp_path, agent="0.2.x")
    assert validate_file(target, require_agent="0.3.0") == 1


def test_require_agent_rejects_malformed_required_value(tmp_path):
    target = _write_payload(tmp_path, agent="0.2.0rc4")
    assert validate_file(target, require_agent="0.2") == 1


def test_require_agent_skipped_when_unset(tmp_path):
    """Without --require-agent the validator only checks schema."""
    target = _write_payload(tmp_path, agent="0.2.0rc4")
    assert validate_file(target) == 0
