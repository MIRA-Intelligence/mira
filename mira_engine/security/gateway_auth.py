"""Gateway access control: per-install bearer token and browser origin policy.

The UI gateway can run shell and filesystem tools, so every non-probe request
must prove it holds the install token. The origin allowlist is defense in
depth against browser pages; the token is the primary control.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

TOKEN_ENV = "MIRA_GATEWAY_TOKEN"
ALLOWED_ORIGINS_ENV = "MIRA_GATEWAY_ALLOWED_ORIGINS"
TOKEN_QUERY_PARAM = "access_token"
TOKEN_FILENAME = "gateway-token"

# Electron renderers loaded from file:// send no Origin on fetch and
# ``Origin: file://`` on WebSocket handshakes (measured on Electron 36).
# The localhost entries cover the Vite dev server.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "file://",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

# Compatibility probes stay open so clients can detect an engine before pairing.
EXEMPT_PATHS = frozenset({"/health", "/version", "/api/health", "/api/version"})


def gateway_token_path(home: Path | None = None) -> Path:
    """Return ``<MIRA_HOME>/runtime/gateway-token``."""
    if home is None:
        from mira_engine.config.loader import get_home_dir

        home = get_home_dir()
    return Path(home) / "runtime" / TOKEN_FILENAME


def _read_token_file(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None


def _write_private(path: Path, token: str) -> Path:
    """Write ``token`` to a fresh 0600 temp file next to ``path``."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return tmp


def ensure_token_file(path: Path | None = None, *, rotate: bool = False) -> str:
    """Return the token stored at ``path``, creating it (mode 0600) when missing.

    Creation publishes a fully written temp file with ``os.link`` so concurrent
    starters either win the race or read the winner's token, never a partial file.
    """
    path = path or gateway_token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not rotate:
        existing = _read_token_file(path)
        if existing:
            return existing
        if path.exists() and not _read_token_file(path):
            # Empty or unreadable file: drop it and compete for creation below.
            path.unlink(missing_ok=True)
    tmp = _write_private(path, secrets.token_urlsafe(32))
    try:
        if rotate:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            except OSError:
                # Filesystems without hard links: fall back to an atomic replace.
                os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    token = _read_token_file(path)
    if not token:
        raise OSError(f"gateway token file is unreadable: {path}")
    return token


def _configured_token_from_file(config_path: Path | None) -> str | None:
    if config_path is None:
        return None
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    gateway = data.get("gateway") if isinstance(data, dict) else None
    if not isinstance(gateway, dict):
        return None
    token = gateway.get("authToken", gateway.get("auth_token"))
    return token.strip() if isinstance(token, str) and token.strip() else None


def resolve_gateway_token(configured: str | None = None, *, home: Path | None = None,
                          create: bool = True) -> str | None:
    """Effective token: env ``MIRA_GATEWAY_TOKEN`` > config ``gateway.authToken`` > token file."""
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    if env_token:
        return env_token
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    path = gateway_token_path(home)
    if create:
        return ensure_token_file(path)
    return _read_token_file(path)


def local_gateway_tokens(config_path: Path | str | None = None) -> list[str]:
    """Non-creating token candidates for local clients such as the service manager.

    A gateway may run with a custom config path outside ``MIRA_HOME``, so both the
    home token file and one next to the config are tried, in resolution order.
    """
    from mira_engine.config.loader import get_home_dir

    config = Path(config_path) if config_path else get_home_dir() / "config.json"
    candidates = [
        os.environ.get(TOKEN_ENV, "").strip() or None,
        _configured_token_from_file(config),
        _read_token_file(gateway_token_path()),
        _read_token_file(gateway_token_path(config.parent)),
    ]
    return list(dict.fromkeys(token for token in candidates if token))


def token_matches(expected: str, presented: str | None) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def presented_token(request: Any) -> str | None:
    """Token from ``Authorization: Bearer`` or the ``access_token`` query parameter."""
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    value = request.query.get(TOKEN_QUERY_PARAM)
    return value if isinstance(value, str) and value else None


def allowed_origins(configured: Iterable[str] | None) -> list[str]:
    """Configured origins (or the defaults) plus ``MIRA_GATEWAY_ALLOWED_ORIGINS`` extras."""
    origins = ([o.strip() for o in configured if isinstance(o, str) and o]
               if isinstance(configured, (list, tuple)) else [])
    if not origins:
        origins = list(DEFAULT_ALLOWED_ORIGINS)
    extra = os.environ.get(ALLOWED_ORIGINS_ENV, "")
    for item in extra.split(","):
        item = item.strip()
        if item and item not in origins:
            origins.append(item)
    return origins


def origin_allowed(origin: str | None, allowed: Iterable[str]) -> bool:
    """Requests without an Origin header come from non-browser clients and pass."""
    if origin is None:
        return True
    allowed = list(allowed)
    return "*" in allowed or origin in allowed


def is_loopback_host(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def warn_if_exposed(host: str, origins: Iterable[str]) -> None:
    if not is_loopback_host(host):
        logger.warning(
            "UI gateway is listening on non-loopback address {}; it is reachable from the "
            "network and protected only by the gateway token", host,
        )
    if "*" in origins:
        logger.warning("UI gateway corsOrigins contains '*'; any website origin is accepted")
