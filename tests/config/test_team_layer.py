"""The Mira Server team layer applied to an engine's config (config/team_layer.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from mira_engine.config import team_layer
from mira_engine.config.loader import load_config, save_config
from mira_engine.config.ui_runtime import (
    apply_ui_runtime_update,
    build_ui_runtime_payload,
    save_ui_runtime_update,
)

TOKEN = "engine-token-123"
PROXY = "http://127.0.0.1:8740/llm"


def _layer(**overrides) -> team_layer.TeamLayer:
    data = {
        "revision": "r1",
        "providers": {"openai": {"api_key": TOKEN, "api_base": f"{PROXY}/openai", "models": ["gpt-5"],
                                 "enabled": True}},
        "defaults": {"provider": "openai", "model": "gpt-5", "temperature": 0.3},
    }
    data.update(overrides)
    return team_layer.parse_layer(data)


@pytest.fixture(autouse=True)
def _clear_layer():
    team_layer.clear_team_layer()
    yield
    team_layer.clear_team_layer()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MIRA_HOME", str(tmp_path))
    path = tmp_path / "config.json"
    monkeypatch.setenv("MIRA_CONFIG_PATH", str(path))
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_no_layer_changes_nothing(config_path: Path) -> None:
    _write(config_path, {"providers": {"openai": {"apiKey": "sk-user"}}})
    config = load_config(config_path)
    assert config.providers.openai.api_key == "sk-user"
    assert "team" not in build_ui_runtime_payload(config, projects_root=config_path.parent,
                                                  config_path=config_path, persisted=False)


def test_team_provider_replaces_the_users_and_is_never_saved(config_path: Path) -> None:
    _write(config_path, {"providers": {"openai": {"apiKey": "sk-user", "models": ["mine"]},
                                       "deepseek": {"apiKey": "sk-deep"}}})
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    assert config.providers.openai.api_key == TOKEN
    assert config.providers.openai.api_base == f"{PROXY}/openai"
    assert config.providers.deepseek.api_key == "sk-deep"  # not a team provider

    save_config(config, config_path)
    saved = _read(config_path)
    assert saved["providers"]["openai"]["apiKey"] == "sk-user"
    assert saved["providers"]["openai"]["models"] == ["mine"]
    assert TOKEN not in config_path.read_text(encoding="utf-8")


def test_team_provider_absent_for_the_user_is_not_saved(config_path: Path) -> None:
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    save_config(config, config_path)
    saved = _read(config_path)
    assert "openai" not in saved["providers"]
    assert TOKEN not in config_path.read_text(encoding="utf-8")


def test_defaults_fill_only_what_the_user_has_not_set(config_path: Path) -> None:
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    assert config.agents.defaults.provider == "openai"
    assert config.agents.defaults.model == "gpt-5"
    assert config.agents.defaults.temperature == 0.3

    _write(config_path, {"agents": {"defaults": {"model": "deepseek/deepseek-chat", "temperature": 0.9}}})
    config = load_config(config_path)
    # The user picked a model, so the team's provider/model pair stays out.
    assert config.agents.defaults.model == "deepseek/deepseek-chat"
    assert config.agents.defaults.provider != "openai"
    assert config.agents.defaults.temperature == 0.9


def test_builtin_defaults_written_at_first_start_do_not_count_as_choices(config_path: Path) -> None:
    from mira_engine.config.schema import Config

    # What an engine writes the first time it starts, before Mira Server pushes the layer.
    save_config(Config(), config_path)
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    assert (config.agents.defaults.provider, config.agents.defaults.model) == ("openai", "gpt-5")
    assert config.agents.defaults.temperature == 0.3


def test_defaults_left_at_the_team_value_keep_following_the_team(config_path: Path) -> None:
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    save_config(config, config_path)
    defaults = _read(config_path)["agents"]["defaults"]
    assert "model" not in defaults and "provider" not in defaults and "temperature" not in defaults

    team_layer.set_team_layer(_layer(defaults={"provider": "openai", "model": "gpt-5-mini"}))
    assert load_config(config_path).agents.defaults.model == "gpt-5-mini"


def test_ui_save_keeps_the_layer_out_and_keeps_user_choices(config_path: Path) -> None:
    _write(config_path, {"providers": {"openai": {"apiKey": "sk-user"}}})
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    body = {
        "runtime": {"provider": "openai", "model": "gpt-5", "temperature": 0.7},
        "providers": {"openai": {"api_base": f"{PROXY}/openai"}, "deepseek": {"api_key": "sk-deep"}},
    }
    apply_ui_runtime_update(config, body, current_projects_root=config_path.parent)
    save_ui_runtime_update(config, body, current_projects_root=config_path.parent, config_path=config_path)

    saved = _read(config_path)
    assert TOKEN not in config_path.read_text(encoding="utf-8")
    assert saved["providers"]["openai"]["apiKey"] == "sk-user"
    assert saved["providers"]["deepseek"]["apiKey"] == "sk-deep"
    defaults = saved["agents"]["defaults"]
    assert defaults["temperature"] == 0.7  # changed by the user
    assert "model" not in defaults and "provider" not in defaults  # still the team's


@pytest.mark.parametrize("update", [
    {"api_key": "sk-other"},
    {"api_base": "https://example.com/v1"},
    {"models": ["other"]},
    {"enabled": False},
])
def test_team_provider_changes_are_locked(config_path: Path, update: dict) -> None:
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    with pytest.raises(team_layer.TeamLockedError) as info:
        apply_ui_runtime_update(config, {"providers": {"openai": update}}, current_projects_root=config_path.parent)
    assert info.value.code == "locked_by_team"


def test_resending_team_provider_values_is_allowed(config_path: Path) -> None:
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    _, changed = apply_ui_runtime_update(
        config,
        {"providers": {"openai": {"api_key": "", "api_base": f"{PROXY}/openai", "models": ["gpt-5"],
                                  "enabled": True}}},
        current_projects_root=config_path.parent,
    )
    assert changed is False


def test_team_model_params_are_enforced_and_locked(config_path: Path) -> None:
    _write(config_path, {"providers": {"modelParams": [{"pattern": "mine", "params": {}}]}})
    rules = [{"pattern": "gpt-5*", "params": {"temperature": None}}]
    team_layer.set_team_layer(_layer(model_params=rules))
    config = load_config(config_path)
    assert [rule.pattern for rule in config.providers.model_params] == ["gpt-5*"]
    with pytest.raises(team_layer.TeamLockedError):
        apply_ui_runtime_update(config, {"model_params": []}, current_projects_root=config_path.parent)
    save_config(config, config_path)
    assert _read(config_path)["providers"]["modelParams"] == [{"pattern": "mine", "params": {}}]


def test_payload_marks_team_providers_and_hides_the_engine_token(config_path: Path) -> None:
    team_layer.set_team_layer(_layer())
    config = load_config(config_path)
    payload = build_ui_runtime_payload(config, projects_root=config_path.parent, config_path=config_path,
                                       persisted=False)
    openai = payload["providers"]["openai"]
    assert openai["managed_by"] == "team" and openai["api_key_configured"] is True
    assert openai["api_key_preview"] is None
    assert "managed_by" not in payload["providers"]["deepseek"]
    assert payload["team"] == {"revision": "r1", "providers": ["openai"], "disabled": [], "model_params": False,
                               "defaults": ["model", "provider", "temperature"]}
    assert TOKEN not in json.dumps(payload)


def test_team_disabled_provider_is_off_and_locked_and_the_users_key_kept(config_path: Path) -> None:
    _write(config_path, {"providers": {"deepseek": {"apiKey": "sk-deep", "enabled": True}}})
    team_layer.set_team_layer(_layer(disabled=["deepseek", "openai_codex"]))
    config = load_config(config_path)
    assert config.providers.deepseek.enabled is False and config.providers.openai_codex.enabled is False
    payload = build_ui_runtime_payload(config, projects_root=config_path.parent, config_path=config_path,
                                       persisted=False)
    assert payload["providers"]["deepseek"]["managed_by"] == "team"
    assert payload["providers"]["deepseek"]["enabled"] is False
    with pytest.raises(team_layer.TeamLockedError, match="turned off"):
        apply_ui_runtime_update(config, {"providers": {"deepseek": {"enabled": True}}},
                                current_projects_root=config_path.parent)
    # Re-sending what the page shows is no change.
    apply_ui_runtime_update(config, {"providers": {"deepseek": {"enabled": False, "api_base": None}}},
                            current_projects_root=config_path.parent)
    save_config(config, config_path)
    assert _read(config_path)["providers"]["deepseek"] == {"apiKey": "sk-deep", "enabled": True}

    team_layer.set_team_layer(_layer())
    assert load_config(config_path).providers.deepseek.enabled is True


@pytest.mark.parametrize("data, message", [
    ([], "object or null"),
    ({"extra": 1}, "unknown key"),
    ({"providers": {"nope": {}}}, "unknown provider"),
    ({"providers": {"openai": {"api_key": 1}}}, "must be strings"),
    ({"providers": {"openai": {"secret": "x"}}}, "must be an object"),
    ({"defaults": {"workspace": "/tmp"}}, "unknown default"),
    ({"disabled": "openai"}, "list of provider names"),
    ({"disabled": ["nope"]}, "unknown provider"),
    ({"model_params": [{"pattern": 1}]}, "pattern"),
])
def test_parse_layer_rejects_malformed_layers(data, message: str) -> None:
    with pytest.raises(team_layer.TeamLayerError, match=message):
        team_layer.parse_layer(data)


def test_parse_layer_null_clears() -> None:
    assert team_layer.parse_layer(None).empty
    assert team_layer.parse_layer({}).empty


# ── the engine endpoint ──────────────────────────────────────────────


def _channel(updated: AsyncMock):
    from mira_engine.channels.ui import UiChannel

    channel = UiChannel.__new__(UiChannel)
    channel._on_runtime_config_updated = updated
    channel.projects_root = Path("/tmp/projects")
    channel._audit = MagicMock()
    return channel


def _request(body) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.json = AsyncMock(return_value=body)
    return request


async def test_endpoint_sets_the_layer_and_reconfigures(config_path: Path) -> None:
    updated = AsyncMock()
    channel = _channel(updated)
    layer = {"revision": "r2", "providers": {"openai": {"api_key": TOKEN, "api_base": f"{PROXY}/openai",
                                                         "models": [], "enabled": None}}}
    response = await channel._handle_team_layer(_request({"layer": layer}))
    assert response.status == 200 and json.loads(response.text) == {"ok": True, "revision": "r2"}
    assert team_layer.get_team_layer().revision == "r2"
    config = updated.await_args.args[0]
    assert config.providers.openai.api_key == TOKEN
    # The audit entry names what the layer manages, never the token.
    assert TOKEN not in json.dumps(channel._audit.call_args.kwargs, default=str)

    # The same layer again: nothing to reconfigure.
    await channel._handle_team_layer(_request({"layer": layer}))
    assert updated.await_count == 1


@pytest.mark.parametrize("body", [[], {}, {"layer": {"bad": 1}}])
async def test_endpoint_rejects_bad_bodies(body) -> None:
    channel = _channel(AsyncMock())
    response = await channel._handle_team_layer(_request(body))
    assert response.status == 400
    assert team_layer.get_team_layer().empty
