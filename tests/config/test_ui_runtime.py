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


def test_build_ui_runtime_payload_returns_raw_and_resolved_workspace(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
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
