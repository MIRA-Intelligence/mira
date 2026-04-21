"""Configuration loading utilities."""

import json
import os
import re
from pathlib import Path

from medpilot.config.schema import Config
from medpilot.security.network import configure_ssrf_whitelist


# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    env_path = os.environ.get("MEDPILOT_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".medpilot" / "config.json"


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
            cfg = Config.model_validate(data)
            configure_ssrf_whitelist(cfg.tools.ssrf_whitelist)
            return cfg
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    cfg = Config()
    configure_ssrf_whitelist(cfg.tools.ssrf_whitelist)
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

    data = config.model_dump(by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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
        web = channels.get("web")
        if isinstance(web, dict):
            gateway = data.get("gateway")
            if not isinstance(gateway, dict):
                gateway = {}
                data["gateway"] = gateway
            if "host" in web and "host" not in gateway:
                gateway["host"] = web["host"]
            if "port" in web and "port" not in gateway:
                gateway["port"] = web["port"]
            web.pop("host", None)
            web.pop("port", None)
    return data
