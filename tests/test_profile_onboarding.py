"""Tests for first-run profile onboarding (USER.local.md + SOUL.local.md)."""

from __future__ import annotations

from mira_engine.config.schema import Config
from mira_engine.profile import (
    apply_profile,
    get_profile_state,
    needs_onboarding,
    render_soul_local,
    render_user_local,
)
from mira_engine.profile.onboarding import SOUL_LOCAL_FILE, USER_LOCAL_FILE


def _config(tmp_path) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path)
    return cfg


def test_needs_onboarding_when_overlay_files_missing(tmp_path):
    cfg = _config(tmp_path)
    assert needs_onboarding(cfg) is True
    # Only one overlay present -> still needs onboarding.
    (tmp_path / USER_LOCAL_FILE).write_text("## Basic Information\n", encoding="utf-8")
    assert needs_onboarding(cfg) is True


def test_flag_short_circuits_detection(tmp_path):
    cfg = _config(tmp_path)
    cfg.profile.onboarded = True
    assert needs_onboarding(cfg) is False


def test_render_user_local_has_basic_information():
    text = render_user_local(
        {"name": "Alice", "timezone": "Asia/Shanghai", "languages": ["English", "中文"]}
    )
    assert text.startswith("## Basic Information")
    assert "- **Name**: Alice" in text
    assert "- **Timezone**: Asia/Shanghai" in text
    assert "- **Preferred language(s)**: English, 中文" in text


def test_render_soul_local_has_identity():
    text = render_soul_local({"name": "Atlas", "identity": "a precise coding partner"})
    assert "I am Atlas, a precise coding partner." in text
    assert "supersedes" in text


def test_render_soul_local_name_only():
    text = render_soul_local({"name": "Atlas"})
    assert "I am Atlas." in text


def test_apply_profile_writes_overlays_and_syncs_config(tmp_path):
    cfg = _config(tmp_path)
    config_path = tmp_path / "config.json"
    result = apply_profile(
        cfg,
        {
            "user": {"name": "Bob", "timezone": "America/New_York", "languages": "English"},
            "agent": {"name": "Nova", "identity": "a research assistant"},
        },
        config_path=config_path,
    )
    assert result["ok"] is True
    # Overlay files (not the base templates) are written.
    assert "- **Name**: Bob" in (tmp_path / USER_LOCAL_FILE).read_text(encoding="utf-8")
    assert "I am Nova, a research assistant." in (tmp_path / SOUL_LOCAL_FILE).read_text(encoding="utf-8")
    assert not (tmp_path / "USER.md").exists()
    assert not (tmp_path / "SOUL.md").exists()
    assert cfg.agents.defaults.timezone == "America/New_York"
    assert cfg.agents.defaults.language == "English"
    assert cfg.profile.onboarded is True
    assert config_path.exists()
    assert needs_onboarding(cfg) is False


def test_apply_profile_requires_agent_name(tmp_path):
    cfg = _config(tmp_path)
    try:
        apply_profile(cfg, {"user": {"name": "Bob"}, "agent": {"name": "  "}}, config_path=tmp_path / "c.json")
    except ValueError as exc:
        assert "agent name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for missing agent name")


def test_get_profile_state_reads_overlays(tmp_path):
    cfg = _config(tmp_path)
    state = get_profile_state(cfg)
    assert state["needs_onboarding"] is True
    assert state["user"]["name"] == ""

    apply_profile(
        cfg,
        {"user": {"name": "Carol", "timezone": "UTC", "languages": "中文"}, "agent": {"name": "Iris"}},
        config_path=tmp_path / "config.json",
    )
    state2 = get_profile_state(cfg)
    assert state2["user"]["name"] == "Carol"
    assert state2["user"]["languages"] == "中文"
    assert state2["agent"]["name"] == "Iris"
