from __future__ import annotations

from pathlib import Path

import pytest

from mira_engine.config.schema import Config
from mira_engine.config.ui_runtime import build_ui_runtime_payload
from mira_engine.providers.factory import make_provider
from mira_engine.providers.litellm_provider import LiteLLMProvider
from mira_engine.providers.registry import PROVIDERS, find_by_name

PLANS = [
    # (provider id, config key, model, expected resolved model, default api base)
    (
        "zhipu_coding_plan",
        "zhipuCodingPlan",
        "glm-5.3",
        "openai/glm-5.3",
        "https://open.bigmodel.cn/api/coding/paas/v4",
    ),
    (
        "kimi_code",
        "kimiCode",
        "kimi-for-coding",
        "openai/kimi-for-coding",
        "https://api.kimi.com/coding/v1",
    ),
    (
        "dashscope_token_plan",
        "dashscopeTokenPlan",
        "qwen3.8-max",
        "openai/qwen3.8-max",
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ),
    (
        "minimax_token_plan",
        "minimaxTokenPlan",
        "MiniMax-M3",
        "openai/MiniMax-M3",
        "https://api.minimaxi.com/v1",
    ),
    (
        "volcengine_agent_plan",
        "volcengineAgentPlan",
        "ark-code-latest",
        "volcengine/ark-code-latest",
        "https://ark.cn-beijing.volces.com/api/plan/v3",
    ),
]


@pytest.fixture(autouse=True)
def _restore_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # LiteLLMProvider exports gateway keys to the process environment.
    for name in ("OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.setenv(name, "")


@pytest.mark.parametrize(("name", "config_key", "model", "resolved", "api_base"), PLANS)
def test_subscription_plan_routes_to_plan_endpoint(
    name: str, config_key: str, model: str, resolved: str, api_base: str
) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": name, "model": model}},
            "providers": {config_key: {"apiKey": "sk-plan"}},
        }
    )

    assert config.get_provider_name() == name
    assert config.get_api_base() == api_base

    provider = make_provider(config)
    assert isinstance(provider, LiteLLMProvider)
    assert provider._gateway is find_by_name(name)
    assert provider._resolve_model(config.agents.defaults.model) == resolved


def test_subscription_plan_honors_regional_api_base() -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "zhipu_coding_plan", "model": "glm-5.3"}},
            "providers": {
                "zhipuCodingPlan": {"apiKey": "sk-plan", "apiBase": "https://api.z.ai/api/coding/paas/v4"}
            },
        }
    )

    assert config.get_api_base() == "https://api.z.ai/api/coding/paas/v4"


def test_plain_model_name_keeps_pay_as_you_go_provider() -> None:
    config = Config()
    config.agents.defaults.provider = "auto"
    config.providers.zhipu.api_key = "sk-payg"
    config.providers.zhipu_coding_plan.api_key = "sk-plan"

    assert config.get_provider_name("glm-5.3") == "zhipu"


def test_kimi_for_coding_model_auto_matches_kimi_code() -> None:
    config = Config()
    config.agents.defaults.provider = "auto"
    config.providers.moonshot.api_key = "sk-payg"
    config.providers.kimi_code.api_key = "sk-plan"

    assert config.get_provider_name("kimi-for-coding") == "kimi_code"
    assert config.get_provider_name("kimi-k2.5") == "moonshot"


def test_xai_matches_grok_models_and_prefixes_for_litellm() -> None:
    config = Config()
    config.agents.defaults.provider = "auto"
    config.agents.defaults.model = "grok-4.5"
    config.providers.xai.api_key = "xai-key"

    assert config.get_provider_name() == "xai"
    provider = make_provider(config)
    assert isinstance(provider, LiteLLMProvider)
    assert provider._resolve_model("grok-4.5") == "xai/grok-4.5"
    # "groq" must not be mistaken for "grok".
    config.providers.groq.api_key = "gsk-key"
    assert config.get_provider_name("groq/llama-3.3-70b-versatile") == "groq"


def test_subscription_flag_marks_plans_and_subscription_logins() -> None:
    subscriptions = {spec.name for spec in PROVIDERS if spec.is_subscription}

    assert subscriptions == {
        "volcengine_coding_plan",
        "volcengine_agent_plan",
        "byteplus_coding_plan",
        "zhipu_coding_plan",
        "kimi_code",
        "dashscope_token_plan",
        "minimax_token_plan",
        "openai_codex",
        "github_copilot",
    }


def test_every_registry_provider_has_a_config_entry() -> None:
    config = Config()
    for spec in PROVIDERS:
        assert config.providers.entry(spec.name) is not None, spec.name


def test_ui_payload_exposes_subscription_flag() -> None:
    payload = build_ui_runtime_payload(
        Config(),
        projects_root=Path("/tmp/workspace"),
        config_path=Path("/tmp/config.json"),
        persisted=True,
    )
    providers = payload["providers"]

    assert providers["kimi_code"]["is_subscription"] is True
    assert providers["kimi_code"]["display_name"] == "Kimi Code"
    assert providers["kimi_code"]["api_key_required"] is True
    assert providers["openai_codex"]["is_subscription"] is True
    assert providers["moonshot"]["is_subscription"] is False
    assert providers["custom"]["is_subscription"] is False
    assert providers["auto"]["is_subscription"] is False
