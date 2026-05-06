from pathlib import Path

from mira_engine.config.schema import Config
from mira_engine.config.ui_runtime import apply_ui_runtime_update, build_ui_runtime_payload


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
