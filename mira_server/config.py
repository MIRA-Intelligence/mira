"""``mira-server.toml``: loading and validation.

Three sections, all optional, with production defaults:

- ``[server]``: listen address, public URL, data dir, browser origins.
- ``[auth]``: PAM (or the dev backend), groups, token lifetimes, login rate limit.
- ``[spawner]``: sudo launcher (or the local dev spawner), timeouts, idle culling.

Unknown keys are errors, so a typo never silently falls back to a default.
"""

from __future__ import annotations

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("/etc/mira-server/mira-server.toml")
DEFAULT_LISTEN = "127.0.0.1:8740"
DEFAULT_DATA_DIR = Path("/var/lib/mira-server")
DEFAULT_LAUNCHER = Path("/usr/local/libexec/mira/engine-launch")
DB_FILENAME = "mira-server.sqlite3"
# Origins the packaged desktop app sends (Electron renderer loaded from file://).
DESKTOP_ORIGINS = ("file://", "null")
# Added in --dev mode for the mira-ui Vite dev server.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

# Host account names. No leading "-" (would read as an option) and no "#"
# (sudo's "#uid" syntax); "." and "@" occur in LDAP/SSSD names.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}$")
_GROUP_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}$")


class ConfigError(Exception):
    """The configuration file is missing, unreadable or invalid."""


def valid_username(name: object) -> bool:
    return isinstance(name, str) and bool(USERNAME_RE.match(name))


def parse_listen(value: str) -> tuple[str, int]:
    """Split ``host:port`` or ``[v6]:port``."""
    value = value.strip()
    if value.startswith("["):
        host, sep, port = value[1:].partition("]:")
    else:
        host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"listen must be host:port (or [ipv6]:port), got {value!r}")
    return host, int(port)


def is_loopback_host(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _check_origin(origin: str) -> str:
    origin = origin.strip()
    if origin in {"file://", "null"}:
        # The packaged desktop app (Electron, file://) sends one of these.
        return origin
    if origin == "*":
        raise ValueError(f"allowed_origins must list explicit origins, not {origin!r}")
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.path not in {"", "/"} \
            or parts.query or parts.fragment:
        raise ValueError(f"not an origin (scheme://host[:port]): {origin!r}")
    return origin_of(origin)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSection(_Section):
    listen: str = DEFAULT_LISTEN
    public_url: str = ""
    data_dir: Path = DEFAULT_DATA_DIR
    # Empty: "file://" and "null" (the desktop app) plus the origin of public_url.
    allowed_origins: list[str] = Field(default_factory=list)
    # Peers whose X-Forwarded-For is trusted for the client IP (the TLS proxy).
    trusted_proxies: list[str] = Field(default_factory=list)

    @field_validator("listen")
    @classmethod
    def _listen(cls, value: str) -> str:
        parse_listen(value)
        return value.strip()

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
            raise ValueError(f"public_url must be an http(s) URL, got {value!r}")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(_check_origin(o) for o in value))

    @field_validator("trusted_proxies")
    @classmethod
    def _proxies(cls, value: list[str]) -> list[str]:
        for item in value:
            ipaddress.ip_address(item.strip())
        return [item.strip() for item in value]

    @property
    def host(self) -> str:
        return parse_listen(self.listen)[0]

    @property
    def port(self) -> int:
        return parse_listen(self.listen)[1]

    def effective_origins(self) -> list[str]:
        if self.allowed_origins:
            return list(self.allowed_origins)
        origins = list(DESKTOP_ORIGINS)
        if self.public_url:
            origins.append(origin_of(self.public_url))
        return origins


class DevUser(_Section):
    name: str
    password_hash: str
    # Member of users_group (runs an engine) and/or admins_group (team settings).
    user: bool = True
    admin: bool = False
    display_name: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not valid_username(value):
            raise ValueError(f"invalid user name {value!r}")
        return value

    @field_validator("password_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        from mira_server.auth.backends import parse_password_hash

        parse_password_hash(value)
        return value


class AuthSection(_Section):
    backend: Literal["pam", "dev"] = "pam"
    pam_service: str = "mira-server"
    users_group: str = "mira-users"
    admins_group: str = "mira-admins"
    access_token_ttl: int = Field(900, ge=60, le=86_400)
    refresh_token_ttl: int = Field(30 * 86_400, ge=600, le=366 * 86_400)
    # Group membership is re-read through NSS at most this often per user (s).
    group_recheck_interval: int = Field(300, ge=0, le=86_400)
    login_max_attempts: int = Field(5, ge=1, le=1000)
    login_window: int = Field(300, ge=1, le=86_400)
    dev_users: list[DevUser] = Field(default_factory=list)

    @field_validator("pam_service")
    @classmethod
    def _service(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            raise ValueError(f"invalid PAM service name {value!r}")
        return value

    @field_validator("users_group", "admins_group")
    @classmethod
    def _group(cls, value: str) -> str:
        if not _GROUP_RE.match(value):
            raise ValueError(f"invalid group name {value!r}")
        return value

    @model_validator(mode="after")
    def _dev_users_unique(self) -> AuthSection:
        names = [u.name for u in self.dev_users]
        if len(names) != len(set(names)):
            raise ValueError("auth.dev_users contains duplicate names")
        for user in self.dev_users:
            if not user.user and not user.admin:
                raise ValueError(f"auth.dev_users {user.name!r} needs user or admin")
        if self.backend == "dev" and not self.dev_users:
            raise ValueError("auth.backend = 'dev' needs at least one [[auth.dev_users]] entry")
        return self


class SpawnerSection(_Section):
    backend: Literal["sudo", "local"] = "sudo"
    launcher: Path = DEFAULT_LAUNCHER
    sudo: Path = Path("/usr/bin/sudo")
    start_timeout: float = Field(90.0, gt=0, le=3600)
    stop_timeout: float = Field(30.0, gt=0, le=3600)
    # Stop an engine Mira Server started after this long without proxied
    # requests or open WebSockets (s); 0 disables culling.
    idle_timeout: int = Field(7200, ge=0)
    # local backend only: interpreter that has mira_engine (default: this one)
    engine_python: str = ""
    # local backend only: MIRA_HOME per dev user, "{user}" is replaced (default: inherited)
    local_mira_home: str = ""
    # Verify the engine's listening socket belongs to the user before proxying.
    # Turning it off is a dev-only escape hatch (needs --dev).
    verify_listener: bool = True

    @field_validator("launcher", "sudo")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"must be an absolute path, got {str(value)!r}")
        return value


class ServerConfig(_Section):
    server: ServerSection = Field(default_factory=ServerSection)
    auth: AuthSection = Field(default_factory=AuthSection)
    spawner: SpawnerSection = Field(default_factory=SpawnerSection)

    @property
    def db_path(self) -> Path:
        return self.server.data_dir / DB_FILENAME

    def dev_only_settings(self) -> list[str]:
        """Settings that make the server unsafe for anything but local development."""
        found = []
        if self.auth.backend == "dev":
            found.append("auth.backend = 'dev'")
        if self.spawner.backend == "local":
            found.append("spawner.backend = 'local'")
        if not self.spawner.verify_listener:
            found.append("spawner.verify_listener = false")
        return found


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(part) for part in err.get("loc", ())) or "(root)"
        lines.append(f"{where}: {err.get('msg')}")
    return "; ".join(lines)


def parse_config(data: dict) -> ServerConfig:
    try:
        return ServerConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {_format_validation_error(exc)}") from None


def load_config(path: Path | str) -> ServerConfig:
    path = Path(path)
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    return parse_config(data)


def check_serve_allowed(config: ServerConfig, *, dev: bool) -> None:
    """Refuse dev-only settings unless ``--dev`` is given and the listener is loopback."""
    dev_settings = config.dev_only_settings()
    if dev_settings and not dev:
        raise ConfigError(f"{', '.join(dev_settings)} is for local development only; "
                          "pass --dev to use it")
    if dev and not is_loopback_host(config.server.host):
        raise ConfigError(f"--dev requires a loopback listen address, not {config.server.host}")
