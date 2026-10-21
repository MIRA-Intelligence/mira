"""LLM providers contributed by plugins (provides.providers)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from mira_engine.config.schema import Config
from mira_engine.plugins import catalog, set_runtime_for_tests
from mira_engine.plugins.state import PluginState
from mira_engine.providers import registry

PROVIDER = '''
from mira_engine.providers.base import LLMProvider, LLMResponse

class EchoProvider(LLMProvider):
    def __init__(self, model, api_key, api_base):
        self.model, self.key, self.base = model, api_key, api_base

    async def chat(self, *a, **kw):
        return LLMResponse(content=f"{self.model}|{self.key}|{self.base}")

    def get_default_model(self):
        return self.model

def create(*, spec, model, api_key, api_base, extra_headers, proxy):
    return EchoProvider(model, api_key, api_base)

def login_status():
    return True, "acct-echo"

async def fetch_models(*, model, api_base, proxy):
    from mira_engine.providers import model_fetch
    return model_fetch._new_cache(provider="echo_llm", models=model_fetch._parse_openai_compatible_models({"data": [{"id": "echo-1"}]}, "echo_llm"))
'''


def _plugin(root: Path, pid: str, spec: dict, **extra) -> Path:
    d = root / pid
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({
        "id": pid, "version": "1.0.0", "default_enabled": True,
        "provides": {"providers": [{
            "spec": spec, "entry": "llm:create", "login_status": "llm:login_status", "models": "llm:fetch_models",
            "endpoint": "https://echo.example", "model_examples": ["echo-1"],
        }]},
        **extra,
    }))
    (d / "llm.py").write_text(textwrap.dedent(PROVIDER))
    return d


@pytest.fixture
def user_plugins(tmp_path, monkeypatch):
    user = tmp_path / "user-plugins"
    user.mkdir()
    monkeypatch.setattr(catalog, "user_plugins_dir", lambda: user)
    monkeypatch.setattr("mira_engine.plugins.loader.user_plugins_dir", lambda: user)
    monkeypatch.setattr(registry, "_plugin_specs_cache", None)
    return user


ECHO = {"name": "echo_llm", "keywords": ["echo-llm"], "display_name": "Echo LLM"}


def _fresh():
    registry._plugin_specs_cache = None
    set_runtime_for_tests(None)


def test_first_party_provider_plugins_supply_codex_and_copilot():
    copilot = registry.find_by_name("github_copilot")
    codex = registry.find_by_name("openai-codex")
    assert copilot.plugin_id == "github-copilot" and copilot.is_oauth and copilot.is_subscription
    assert codex.plugin_id == "openai-codex" and codex.default_api_base == "https://chatgpt.com/backend-api"
    assert registry.find_by_model("github-copilot/gpt-4.1").name == "github_copilot"
    assert "github_copilot" in Config().providers.provider_names()


def test_make_provider_builds_codex_and_copilot_through_their_plugins():
    from mira_engine.providers.factory import make_provider

    cfg = Config.model_validate({"agents": {"defaults": {"provider": "github_copilot", "model": "github-copilot/gpt-4.1"}}})
    provider = make_provider(cfg)
    assert type(provider).__module__ == "mira_plugins.github_copilot.provider"
    cfg = Config.model_validate({"agents": {"defaults": {"model": "openai-codex/gpt-5.1-codex"}}})
    assert type(make_provider(cfg)).__name__ == "OpenAICodexProvider"


def test_a_verified_user_plugin_can_add_a_provider(user_plugins):
    _plugin(user_plugins, "echo-plugin", ECHO)
    PluginState().set_registry_tiers({"echo-plugin": "verified"})
    _fresh()
    spec = registry.find_by_name("echo_llm")
    assert spec is not None and spec.plugin_id == "echo-plugin"

    cfg = Config.model_validate({
        "agents": {"defaults": {"provider": "echo_llm", "model": "echo-llm/echo-1"}},
        "providers": {"echoLlm": {"apiKey": "sk-echo", "apiBase": "https://echo.example/v1"}},
    })
    assert cfg.providers.entry("echo_llm").api_key == "sk-echo"
    from mira_engine.providers.factory import make_provider

    provider = make_provider(cfg)
    assert type(provider).__name__ == "EchoProvider"
    assert (provider.model, provider.key, provider.base) == ("echo-llm/echo-1", "sk-echo", "https://echo.example/v1")
    # The section is saved with the rest of the config.
    dumped = cfg.model_dump(by_alias=True)["providers"]
    assert dumped["echoLlm"]["apiKey"] == "sk-echo"


def test_unrated_or_community_plugins_cannot_add_providers(user_plugins):
    _plugin(user_plugins, "sketchy", {**ECHO, "name": "sketchy_llm"})
    _fresh()
    assert registry.find_by_name("sketchy_llm") is None  # experimental: not listed
    PluginState().set_registry_tiers({"sketchy": "community"})
    _fresh()
    assert registry.find_by_name("sketchy_llm") is None
    from mira_engine.plugins import get_runtime

    PluginState().set_enabled("sketchy", True)
    set_runtime_for_tests(None)
    assert "need a verified or core-bundled" in get_runtime().get("sketchy").reason


def test_a_plugin_cannot_take_a_core_provider_name(user_plugins):
    _plugin(user_plugins, "fake-openai", {**ECHO, "name": "openai"})
    PluginState().set_registry_tiers({"fake-openai": "verified"})
    _fresh()
    assert registry.find_by_name("openai").plugin_id == ""


def test_plugin_provider_hooks_for_status_models_and_cli_metadata(user_plugins):
    import asyncio

    from mira_engine.cli import commands
    from mira_engine.providers.model_fetch import fetch_provider_models

    _plugin(user_plugins, "echo-plugin", ECHO)
    PluginState().set_registry_tiers({"echo-plugin": "verified"})
    _fresh()
    assert commands._oauth_login_status("echo_llm") == (True, "acct-echo")
    assert commands._provider_model_examples("echo_llm") == ("echo-1",)
    assert commands._provider_probe_url(registry.find_by_name("echo_llm"), None) == "https://echo.example"
    cache = asyncio.run(fetch_provider_models(Config(), "echo_llm"))
    assert [m.id for m in cache.models] == ["echo-1"]


def test_ui_lists_and_saves_plugin_providers(user_plugins, tmp_path):
    from mira_engine.config.ui_runtime import apply_ui_runtime_update, build_ui_runtime_payload

    _plugin(user_plugins, "echo-plugin", ECHO)
    PluginState().set_registry_tiers({"echo-plugin": "verified"})
    _fresh()
    cfg = Config()
    payload = build_ui_runtime_payload(cfg, projects_root=tmp_path, config_path=tmp_path / "config.json", persisted=False)
    assert "echo_llm" in payload["providers"]
    apply_ui_runtime_update(cfg, {"providers": {"echo_llm": {"api_key": "sk-ui"}}}, current_projects_root=tmp_path)
    assert cfg.providers.entry("echo_llm").api_key == "sk-ui"
    assert cfg.model_dump(by_alias=True)["providers"]["echoLlm"]["apiKey"] == "sk-ui"
