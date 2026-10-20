"""Access control for the OpenAI-compatible API server (``mira serve``).

``/v1`` drives the same tool-using agent as the UI gateway, so it requires a
bearer key. Browser requests are refused unless their origin is allowlisted.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Iterable

from mira_engine.security.gateway_auth import resolve_gateway_token

API_KEYS_ENV = "MIRA_API_KEYS"
API_ALLOWED_ORIGINS_ENV = "MIRA_API_ALLOWED_ORIGINS"

EXEMPT_PATHS = frozenset({"/health"})


def _split_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _clean(values: Iterable[str] | None) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def resolve_api_keys(configured: Iterable[str] | None,
                     gateway_auth_token: str | None = None) -> tuple[list[str], str]:
    """Accepted keys and their source.

    Precedence: env ``MIRA_API_KEYS`` > config ``api.keys`` > the install's gateway
    token, so ``mira gateway-token`` prints a working key with no extra setup.
    """
    if keys := _split_env(API_KEYS_ENV):
        return list(dict.fromkeys(keys)), API_KEYS_ENV
    if keys := _clean(configured):
        return list(dict.fromkeys(keys)), "api.keys"
    return [resolve_gateway_token(gateway_auth_token)], "gateway token"


def api_allowed_origins(configured: Iterable[str] | None) -> list[str]:
    """Configured browser origins plus ``MIRA_API_ALLOWED_ORIGINS`` extras; none by default."""
    origins = _clean(configured)
    for item in _split_env(API_ALLOWED_ORIGINS_ENV):
        if item not in origins:
            origins.append(item)
    return origins


def bearer_token(request: Any) -> str | None:
    """Key from ``Authorization: Bearer``; query-string keys are not accepted."""
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def key_matches(keys: Iterable[str], presented: str | None) -> bool:
    if not presented:
        return False
    data = presented.encode("utf-8")
    # Compare against every key so timing does not reveal which one matched.
    return any([hmac.compare_digest(key.encode("utf-8"), data) for key in keys if key])
