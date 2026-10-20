"""Header, query and frame rewriting for the ``/api/*`` and ``/ws`` proxies.

Requests to the engine never carry the client's credentials: ``Authorization``
and ``Cookie`` are dropped, as are hop-by-hop headers, ``Origin`` (Mira Server
already checked it; the engine's own allowlist only knows local origins) and any
client-supplied ``X-Mira-Server-*`` or ``X-Mira-Request-Id`` header. Mira Server
adds the engine token, the authenticated user and role, and its request id.
"""

from __future__ import annotations

import json
from typing import Mapping
from urllib.parse import unquote_plus

from multidict import CIMultiDict

TOKEN_QUERY_PARAM = "access_token"

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})
_DROP_REQUEST = HOP_BY_HOP | {"host", "authorization", "cookie", "origin", "x-mira-request-id"}
_DROP_RESPONSE = HOP_BY_HOP | {"set-cookie", "www-authenticate", "server", "date"}

USER_HEADER = "X-Mira-Server-User"
ROLE_HEADER = "X-Mira-Server-Role"
REQUEST_ID_HEADER = "X-Mira-Request-Id"


def _connection_listed(headers: Mapping[str, str]) -> set[str]:
    listed: set[str] = set()
    for key, value in headers.items():
        if key.lower() == "connection":
            listed.update(item.strip().lower() for item in value.split(",") if item.strip())
    return listed


def forward_request_headers(headers: Mapping[str, str], *, engine_token: str, user: str, role: str,
                            request_id: str) -> CIMultiDict[str]:
    drop = _DROP_REQUEST | _connection_listed(headers)
    out: CIMultiDict[str] = CIMultiDict()
    for key, value in headers.items():
        lower = key.lower()
        if lower in drop or lower.startswith("x-mira-server-"):
            continue
        out.add(key, value)
    out["Authorization"] = f"Bearer {engine_token}"
    out[USER_HEADER] = user
    out[ROLE_HEADER] = role
    out[REQUEST_ID_HEADER] = request_id
    return out


def forward_response_headers(headers: Mapping[str, str]) -> CIMultiDict[str]:
    drop = _DROP_RESPONSE | _connection_listed(headers)
    out: CIMultiDict[str] = CIMultiDict()
    for key, value in headers.items():
        lower = key.lower()
        if lower in drop or lower.startswith("access-control-"):
            continue
        out.add(key, value)
    return out


def strip_token_query(raw_query: str) -> str:
    """Drop every ``access_token`` parameter, keeping the rest byte for byte."""
    if not raw_query:
        return ""
    kept = []
    for part in raw_query.split("&"):
        if not part:
            continue
        name = unquote_plus(part.split("=", 1)[0])
        if name == TOKEN_QUERY_PARAM:
            continue
        kept.append(part)
    return "&".join(kept)


def rewrite_client_frame(text: str, username: str) -> str:
    """Set ``user_id`` to the authenticated user in JSON-object frames; pass others through."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return text
    if not isinstance(data, dict):
        return text
    data["user_id"] = username
    return json.dumps(data, ensure_ascii=False)
