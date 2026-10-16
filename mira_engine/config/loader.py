"""Configuration loading utilities."""

import json
import os
import re
from pathlib import Path

from loguru import logger

from mira_engine.config import team_layer
from mira_engine.config.schema import Config
from mira_engine.providers.registry import set_user_model_param_rules
from mira_engine.security.gateway_auth import ALLOWED_ORIGINS_ENV, DEFAULT_ALLOWED_ORIGINS
from mira_engine.security.host_policy import enforce as enforce_host_policy
from mira_engine.security.host_policy import restore_user_values
from mira_engine.security.network import configure_ssrf_whitelist


def _apply_runtime_registry_config(config: Config) -> None:
    """Push config-derived runtime data into the (process-global) registry."""
    set_user_model_param_rules(
        [(rule.pattern, rule.params) for rule in config.providers.model_params]
    )


# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None

# load_config runs many times per process; report the CORS migration once.
_legacy_cors_migration_logged = False


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_home_dir() -> Path:
    """Return the MIRA home directory.

    Resolution order:
    1. ``MIRA_HOME`` environment variable (``~`` is expanded), if set.
    2. The default ``~/.mira``.
    """
    env_home = os.environ.get("MIRA_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".mira"


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    env_path = os.environ.get("MIRA_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return get_home_dir() / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            team_values = team_layer.apply_to_raw(data)
            cfg = Config.model_validate(data)
            team_layer.attach(cfg, team_values)
            configure_ssrf_whitelist(cfg.tools.ssrf_whitelist)
            _apply_runtime_registry_config(cfg)
            enforce_host_policy(cfg)
            return cfg
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    data = {}
    team_values = team_layer.apply_to_raw(data)
    cfg = Config.model_validate(data)
    team_layer.attach(cfg, team_values)
    configure_ssrf_whitelist(cfg.tools.ssrf_whitelist)
    _apply_runtime_registry_config(cfg)
    enforce_host_policy(cfg)
    return cfg


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = restore_user_values(config, config.model_dump(by_alias=True))
    data = team_layer.restore_team_values(config, data)

    # config.json is shared across all instances; serialize concurrent writes
    # and replace atomically so a crashed/racing writer never leaves a partial
    # (unparseable) config on disk.
    from mira_engine.utils.locks import locked_write_text

    locked_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def resolve_config_env_vars(config: Config) -> Config:
    """Return config copy with ${VAR} references resolved from environment."""
    data = config.model_dump(mode="json", by_alias=True)
    data = _resolve_env_vars(data)
    return Config.model_validate(data)


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ${VAR} patterns in string values."""
    if isinstance(obj, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"Environment variable '{name}' referenced in config is not set")
    return value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    channels = data.get("channels", {})
    if isinstance(channels, dict):
        qq = channels.get("qq")
        if isinstance(qq, dict) and "msgFormat" not in qq:
            qq["msgFormat"] = "plain"
        # The "web" channel was renamed to "ui" for clarity. Preserve any
        # existing user config by promoting "web" -> "ui" when "ui" isn't
        # already present, then merge any straggler fields.
        legacy_web = channels.pop("web", None) if "web" in channels else None
        if isinstance(legacy_web, dict):
            existing_ui = channels.get("ui")
            if isinstance(existing_ui, dict):
                for k, v in legacy_web.items():
                    existing_ui.setdefault(k, v)
            else:
                channels["ui"] = legacy_web
        ui = channels.get("ui")
        if isinstance(ui, dict):
            gateway = data.get("gateway")
            if not isinstance(gateway, dict):
                gateway = {}
                data["gateway"] = gateway
            if "host" in ui and "host" not in gateway:
                gateway["host"] = ui["host"]
            if "port" in ui and "port" not in gateway:
                gateway["port"] = ui["port"]
            ui.pop("host", None)
            ui.pop("port", None)
            _migrate_legacy_cors_origins(ui)
    return data


def _migrate_legacy_cors_origins(ui: dict) -> None:
    """Replace the pre-gateway-auth wildcard default with the app origins.

    Older versions saved the whole config, so a bare ``["*"]`` is the old
    default rather than a choice. Mixed lists are left alone; a deliberate
    wildcard is kept by setting ``MIRA_GATEWAY_ALLOWED_ORIGINS=*``.
    """
    global _legacy_cors_migration_logged
    for key in ("corsOrigins", "cors_origins"):
        if ui.get(key) != ["*"]:
            continue
        ui[key] = list(DEFAULT_ALLOWED_ORIGINS)
        if not _legacy_cors_migration_logged:
            _legacy_cors_migration_logged = True
            logger.info(
                "Replaced legacy UI corsOrigins ['*'] with the app defaults; "
                "set {}=* to keep accepting any origin", ALLOWED_ORIGINS_ENV,
            )
