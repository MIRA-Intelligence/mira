from pathlib import Path

from mira_engine.config.schema import Config
from mira_engine.config.ui_runtime import (
    apply_ui_runtime_update,
    apply_ui_runtime_update_to_raw_data,
    build_ui_runtime_payload,
)


def test_build_ui_runtime_payload_includes_dynamic_provider_metadata() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "deepseek"
    cfg.agents.defaults.model = "deepseek/deepseek-chat"
    cfg.providers.deepseek.api_key = "sk-deepseek"
    cfg.providers.proxy = "http://127.0.0.1:7890"

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=True,
    )

    assert payload["runtime"]["setup_required"] is False
    assert payload["runtime"]["setup_message"] is None
    assert payload["runtime"]["setup_code"] is None
    assert payload["runtime"]["setup_subject"] is None
    assert payload["providers"]["auto"]["display_name"] == "Auto-detect"
    assert payload["providers"]["deepseek"]["display_name"] == "DeepSeek"
    assert payload["providers"]["deepseek"]["api_key_required"] is True
    assert payload["providers"]["deepseek"]["api_key_configured"] is True
    assert "proxy" not in payload["providers"]
    assert payload["provider_proxy"] == "http://127.0.0.1:7890"


def test_build_ui_runtime_payload_includes_nvidia_provider_metadata() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "nvidia"
    cfg.agents.defaults.model = "nvidia/deepseek-ai/deepseek-v4-pro"
    cfg.providers.nvidia.api_key = "nvapi-test-key"

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=True,
    )

    assert payload["runtime"]["setup_required"] is False
    assert payload["providers"]["nvidia"]["display_name"] == "NVIDIA"
    assert payload["providers"]["nvidia"]["api_key_required"] is True
    assert payload["providers"]["nvidia"]["api_base_required"] is False
    assert payload["providers"]["nvidia"]["default_api_base"] == "https://inference-api.nvidia.com/v1"


def test_build_ui_runtime_payload_returns_raw_and_resolved_workspace(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = Config()
    cfg.agents.defaults.workspace = "~/.mira/workspace"

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=home / ".mira" / "workspace",
        config_path=home / ".mira" / "config.json",
        persisted=False,
    )

    assert payload["projects_root"] == str(home / ".mira" / "workspace")
    assert payload["runtime"]["workspace"] == "~/.mira/workspace"
    assert payload["runtime"]["workspace_resolved"] == str(home / ".mira" / "workspace")


def test_build_ui_runtime_payload_marks_missing_required_provider_config() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "azure_openai"
    cfg.agents.defaults.model = "gpt-4.1"

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=True,
    )

    assert payload["runtime"]["setup_required"] is True
    assert "Azure OpenAI requires API Base" in payload["runtime"]["setup_message"]
    assert payload["runtime"]["setup_code"] == "missing_api_base"
    assert payload["runtime"]["setup_subject"] == "Azure OpenAI"


def test_build_ui_runtime_payload_marks_bundle_placeholder_as_setup_required() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "custom"
    cfg.agents.defaults.model = "custom/mira-ui-bundle-setup"
    cfg.providers.custom.api_base = "http://127.0.0.1:9/v1"

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=True,
    )

    assert payload["runtime"]["setup_required"] is True
    assert "model access is still unconfigured" in payload["runtime"]["setup_message"]
    assert payload["runtime"]["setup_code"] == "missing_api_base"
    assert payload["runtime"]["setup_subject"] == "Custom"


def test_apply_ui_runtime_update_accepts_new_provider_names() -> None:
    cfg = Config()

    next_root, changed = apply_ui_runtime_update(
        cfg,
        {
            "runtime": {
                "provider": "deepseek",
                "model": "deepseek/deepseek-chat",
            },
            "providers": {
                "deepseek": {
                    "api_key": "sk-new-key",
                }
            },
        },
        current_projects_root=Path("/tmp/workspace"),
    )

    assert changed is True
    assert next_root == Path("/tmp/workspace").resolve()
    assert cfg.agents.defaults.provider == "deepseek"
    assert cfg.agents.defaults.model == "deepseek/deepseek-chat"
    assert cfg.providers.deepseek.api_key == "sk-new-key"


def test_apply_ui_runtime_update_noop_does_not_flag_changed() -> None:
    cfg = Config()
    defaults = cfg.agents.defaults

    # Re-post the runtime block with the values already in config (the shape the
    # UI sends when only a frontend-only preference was toggled).
    next_root, changed = apply_ui_runtime_update(
        cfg,
        {
            "runtime": {
                "workspace": defaults.workspace,
                "provider": defaults.provider,
                "model": defaults.model,
                "reasoning_effort": defaults.reasoning_effort,
                "max_tool_iterations": defaults.max_tool_iterations,
                "restrict_to_workspace": cfg.tools.restrict_to_workspace,
            },
            "providers": {},
        },
        current_projects_root=Path(defaults.workspace).expanduser(),
    )

    assert changed is False


def test_apply_ui_runtime_update_accepts_global_provider_proxy() -> None:
    cfg = Config()

    next_root, changed = apply_ui_runtime_update(
        cfg,
        {"providers": {"proxy": " http://127.0.0.1:7890 "}},
        current_projects_root=Path("/tmp/workspace"),
    )

    assert changed is True
    assert next_root == Path("/tmp/workspace").resolve()
    assert cfg.providers.proxy == "http://127.0.0.1:7890"


def test_apply_ui_runtime_update_to_raw_data_preserves_routing_models() -> None:
    data = {
        "agents": {
            "defaults": {
                "workspace": "/tmp/old",
                "provider": "openrouter",
                "model": ["claude-3-opus", "anthropic/claude-sonnet-4-5"],
                "routeModel": ["openai/gpt-4.1-mini", "openai/gpt-4.1-nano"],
                "smallModel": ["deepseek/deepseek-chat", "openai/gpt-4.1-mini"],
                "mediumModel": "anthropic/claude-sonnet-4-5",
                "largeModel": "anthropic/claude-opus-4-5",
            }
        },
        "providers": {
            "openrouter": {
                "apiKey": "existing-key",
            }
        },
    }

    next_root, changed = apply_ui_runtime_update_to_raw_data(
        data,
        {
            "runtime": {
                "workspace": "/tmp/new",
                "provider": "openrouter",
                "model": "openrouter/claude-3-opus",
                "max_tool_iterations": 64,
            },
            "providers": {
                "openrouter": {
                    "api_base": "https://openrouter.ai/api/v1",
                }
            },
        },
        current_projects_root=Path("/tmp/old"),
    )

    defaults = data["agents"]["defaults"]
    assert changed is True
    assert next_root == Path("/tmp/new").resolve()
    assert defaults["workspace"] == "/tmp/new"
    assert defaults["model"] == ["claude-3-opus", "anthropic/claude-sonnet-4-5"]
    assert defaults["routeModel"] == ["openai/gpt-4.1-mini", "openai/gpt-4.1-nano"]
    assert defaults["smallModel"] == ["deepseek/deepseek-chat", "openai/gpt-4.1-mini"]
    assert defaults["mediumModel"] == "anthropic/claude-sonnet-4-5"
    assert defaults["largeModel"] == "anthropic/claude-opus-4-5"
    assert defaults["maxToolIterations"] == 64
    assert data["providers"]["openrouter"]["apiKey"] == "existing-key"
    assert data["providers"]["openrouter"]["apiBase"] == "https://openrouter.ai/api/v1"


def test_build_ui_runtime_payload_includes_temperature() -> None:
    cfg = Config()
    cfg.agents.defaults.temperature = 0.42

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=False,
    )

    assert payload["runtime"]["temperature"] == 0.42


def test_apply_ui_runtime_update_sets_temperature() -> None:
    cfg = Config()

    _, changed = apply_ui_runtime_update(
        cfg,
        {"runtime": {"temperature": 1}},
        current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
    )

    assert changed is True
    assert cfg.agents.defaults.temperature == 1.0


def test_apply_ui_runtime_update_clears_temperature_when_null() -> None:
    cfg = Config()
    cfg.agents.defaults.temperature = 0.7

    _, changed = apply_ui_runtime_update(
        cfg,
        {"runtime": {"temperature": None}},
        current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
    )

    assert changed is True
    assert cfg.agents.defaults.temperature is None


def test_apply_ui_runtime_update_clears_temperature_when_empty_string() -> None:
    cfg = Config()
    cfg.agents.defaults.temperature = 0.7

    _, changed = apply_ui_runtime_update(
        cfg,
        {"runtime": {"temperature": ""}},
        current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
    )

    assert changed is True
    assert cfg.agents.defaults.temperature is None


def test_apply_ui_runtime_update_rejects_out_of_range_temperature() -> None:
    cfg = Config()

    for bad in (-0.1, 2.1, "hot", True):
        try:
            apply_ui_runtime_update(
                cfg,
                {"runtime": {"temperature": bad}},
                current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
            )
        except ValueError:
            continue
        raise AssertionError(f"temperature {bad!r} should have been rejected")


def test_apply_ui_runtime_update_to_raw_data_sets_temperature() -> None:
    data: dict = {"agents": {"defaults": {}}}

    _, changed = apply_ui_runtime_update_to_raw_data(
        data,
        {"runtime": {"temperature": 0.9}},
        current_projects_root=Path("/tmp/workspace"),
    )

    assert changed is True
    assert data["agents"]["defaults"]["temperature"] == 0.9


def test_build_ui_runtime_payload_exposes_models_and_configured() -> None:
    cfg = Config()
    cfg.providers.deepseek.api_key = "sk-deepseek"
    cfg.providers.deepseek.models = ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=False,
    )

    deepseek = payload["providers"]["deepseek"]
    assert deepseek["models"] == ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]
    assert deepseek["configured"] is True
    # A provider with no key/base/models and not local/oauth is not "configured".
    assert payload["providers"]["openai"]["configured"] is False
    assert payload["providers"]["openai"]["models"] == []


def test_build_ui_runtime_payload_includes_team_role_bindings() -> None:
    cfg = Config()
    cfg.agents.defaults.supervisor_provider = "anthropic"
    cfg.agents.defaults.supervisor_model = "anthropic/claude-opus-4-5"
    cfg.agents.defaults.student_provider = "deepseek"
    cfg.agents.defaults.student_model = "deepseek/deepseek-chat"

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=False,
    )

    runtime = payload["runtime"]
    assert runtime["supervisor_provider"] == "anthropic"
    assert runtime["supervisor_model"] == "anthropic/claude-opus-4-5"
    assert runtime["student_provider"] == "deepseek"
    assert runtime["student_model"] == "deepseek/deepseek-chat"
    # Unset role inherits: provider "auto", model null.
    assert runtime["critic_provider"] == "auto"
    assert runtime["critic_model"] is None


def test_apply_ui_runtime_update_sets_provider_models() -> None:
    cfg = Config()

    _, changed = apply_ui_runtime_update(
        cfg,
        {"providers": {"deepseek": {"models": ["deepseek/deepseek-chat", "  ", "deepseek/deepseek-reasoner"]}}},
        current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
    )

    assert changed is True
    # Blank entries are dropped.
    assert cfg.providers.deepseek.models == ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]


def test_apply_ui_runtime_update_sets_role_bindings() -> None:
    cfg = Config()

    _, changed = apply_ui_runtime_update(
        cfg,
        {
            "runtime": {
                "critic_provider": "anthropic",
                "critic_model": "anthropic/claude-opus-4-5",
                "student_model": "",
            }
        },
        current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
    )

    assert changed is True
    assert cfg.agents.defaults.critic_provider == "anthropic"
    assert cfg.agents.defaults.critic_model == "anthropic/claude-opus-4-5"
    assert cfg.agents.defaults.student_model is None


def test_apply_ui_runtime_update_rejects_unknown_role_provider() -> None:
    cfg = Config()

    try:
        apply_ui_runtime_update(
            cfg,
            {"runtime": {"supervisor_provider": "not-a-provider"}},
            current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
        )
    except ValueError as exc:
        assert "unsupported provider" in str(exc)
    else:
        raise AssertionError("unknown role provider should have been rejected")


def test_apply_ui_runtime_update_to_raw_data_sets_role_and_models() -> None:
    data: dict = {"agents": {"defaults": {}}, "providers": {}}

    _, changed = apply_ui_runtime_update_to_raw_data(
        data,
        {
            "runtime": {
                "supervisor_provider": "anthropic",
                "supervisor_model": "anthropic/claude-opus-4-5",
            },
            "providers": {"deepseek": {"models": ["deepseek/deepseek-chat"]}},
        },
        current_projects_root=Path("/tmp/workspace"),
    )

    assert changed is True
    defaults = data["agents"]["defaults"]
    assert defaults["supervisorProvider"] == "anthropic"
    assert defaults["supervisorModel"] == "anthropic/claude-opus-4-5"
    assert data["providers"]["deepseek"]["models"] == ["deepseek/deepseek-chat"]


def test_build_ui_runtime_payload_enabled_defaults_to_configured() -> None:
    cfg = Config()
    cfg.providers.deepseek.api_key = "sk-deepseek"

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=False,
    )

    # Unset enabled => follows the derived "configured" state.
    assert payload["providers"]["deepseek"]["enabled"] is True
    assert payload["providers"]["openai"]["enabled"] is False


def test_build_ui_runtime_payload_enabled_explicit_overrides_configured() -> None:
    cfg = Config()
    # Configured (has a key) but explicitly disabled.
    cfg.providers.deepseek.api_key = "sk-deepseek"
    cfg.providers.deepseek.enabled = False
    # Not configured but explicitly enabled.
    cfg.providers.openai.enabled = True

    payload = build_ui_runtime_payload(
        cfg,
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=False,
    )

    assert payload["providers"]["deepseek"]["configured"] is True
    assert payload["providers"]["deepseek"]["enabled"] is False
    assert payload["providers"]["openai"]["enabled"] is True


def test_apply_ui_runtime_update_sets_provider_enabled() -> None:
    cfg = Config()

    _, changed = apply_ui_runtime_update(
        cfg,
        {"providers": {"openai": {"enabled": True}, "deepseek": {"enabled": False}}},
        current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
    )

    assert changed is True
    assert cfg.providers.openai.enabled is True
    assert cfg.providers.deepseek.enabled is False


def test_apply_ui_runtime_update_rejects_non_boolean_enabled() -> None:
    cfg = Config()

    try:
        apply_ui_runtime_update(
            cfg,
            {"providers": {"openai": {"enabled": "yes"}}},
            current_projects_root=Path(cfg.agents.defaults.workspace).expanduser(),
        )
    except ValueError as exc:
        assert "enabled must be a boolean" in str(exc)
    else:
        raise AssertionError("non-boolean enabled should have been rejected")


def test_apply_ui_runtime_update_to_raw_data_sets_enabled() -> None:
    data: dict = {"agents": {"defaults": {}}, "providers": {}}

    _, changed = apply_ui_runtime_update_to_raw_data(
        data,
        {"providers": {"openai": {"enabled": True}}},
        current_projects_root=Path("/tmp/workspace"),
    )

    assert changed is True
    assert data["providers"]["openai"]["enabled"] is True
