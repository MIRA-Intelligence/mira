"""Tests for first-run profile onboarding (USER.md + SOUL.md)."""

from __future__ import annotations

from mira_engine.config.schema import Config
from mira_engine.profile import (
    apply_profile,
    get_profile_state,
    needs_onboarding,
    render_soul_md,
    render_user_md,
)
from mira_engine.profile.onboarding import load_template


def _config(tmp_path) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(tmp_path)
    return cfg


def test_needs_onboarding_when_files_default_or_missing(tmp_path):
    cfg = _config(tmp_path)
    # No files yet -> needs onboarding.
    assert needs_onboarding(cfg) is True
    # Default template files still count as "not set up".
    (tmp_path / "USER.md").write_text(load_template("USER.md"), encoding="utf-8")
    (tmp_path / "SOUL.md").write_text(load_template("SOUL.md"), encoding="utf-8")
    assert needs_onboarding(cfg) is True


def test_flag_short_circuits_detection(tmp_path):
    cfg = _config(tmp_path)
    cfg.profile.onboarded = True
    assert needs_onboarding(cfg) is False


def test_render_user_md_substitutes_basic_information():
    text = render_user_md({"name": "Alice", "timezone": "Asia/Shanghai", "languages": ["English", "中文"]})
    assert "- **Name**: Alice" in text
    assert "- **Timezone**: Asia/Shanghai" in text
    assert "- **Preferred language(s)**: English, 中文" in text
    # Optional sections keep the template defaults.
    assert "## Topics of Interest" in text
    assert "## Preferences" in text


def test_render_soul_md_substitutes_identity():
    text = render_soul_md({"name": "Atlas", "identity": "a precise coding partner"})
    assert "I am Atlas, a precise coding partner." in text
    # Stable principles are preserved.
    assert "## Stable Principles (All Profiles)" in text


def test_render_soul_md_keeps_base_descriptor_when_identity_blank():
    text = render_soul_md({"name": "Atlas"})
    assert text.count("I am Atlas") == 1
    assert "I am Atlas," in text  # descriptor inherited from template


def test_apply_profile_writes_files_and_syncs_config(tmp_path):
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
    assert "- **Name**: Bob" in (tmp_path / "USER.md").read_text(encoding="utf-8")
    assert "I am Nova, a research assistant." in (tmp_path / "SOUL.md").read_text(encoding="utf-8")
    assert cfg.agents.defaults.timezone == "America/New_York"
    assert cfg.agents.defaults.language == "English"
    assert cfg.profile.onboarded is True
    assert config_path.exists()
    # No longer needs onboarding once applied.
    assert needs_onboarding(cfg) is False


def test_apply_profile_requires_agent_name(tmp_path):
    cfg = _config(tmp_path)
    try:
        apply_profile(cfg, {"user": {"name": "Bob"}, "agent": {"name": "  "}}, config_path=tmp_path / "c.json")
    except ValueError as exc:
        assert "agent name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for missing agent name")


def test_get_profile_state_cleans_placeholders(tmp_path):
    cfg = _config(tmp_path)
    # Default template -> placeholders should surface as empty for prefilling.
    state = get_profile_state(cfg)
    assert state["needs_onboarding"] is True
    assert state["user"]["name"] == ""
    assert state["user"]["timezone"] == ""
    # After applying, the state reflects the saved values.
    apply_profile(
        cfg,
        {"user": {"name": "Carol", "timezone": "UTC", "languages": "中文"}, "agent": {"name": "Iris"}},
        config_path=tmp_path / "config.json",
    )
    state2 = get_profile_state(cfg)
    assert state2["user"]["name"] == "Carol"
    assert state2["user"]["languages"] == "中文"
    assert state2["agent"]["name"] == "Iris"
