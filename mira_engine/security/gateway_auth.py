"""Per-install authentication for the single-user gateway."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{16,512}\Z")
_PUBLIC_PATHS = {"/health", "/version", "/api/health", "/api/version"}


def load_gateway_token(home: Path) -> str:
    """Create an owner-only token, or securely read the existing token.

    Keep the path and format compatible with mira-ui's Electron token bridge.
    Invalid existing files fail startup rather than disabling authentication.
    """
    path = home / "runtime" / "gateway-token"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise ValueError("Gateway token file must not be a symbolic link")
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "r", encoding="ascii") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Gateway token must be a regular file without hard links")
            if os.name == "posix":
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise ValueError("Gateway token must be owned by this user with mode 0600")
            token = stream.read(1024).strip()
        if not _TOKEN_PATTERN.fullmatch(token):
            raise ValueError("Invalid gateway token file; authentication cannot start")
        return token
    else:
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
        return token


def gateway_auth_middleware(token: str) -> Callable:
    """Require a gateway-admin token; this is not a multi-user ACL system."""
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("A valid gateway token is required")

    @web.middleware
    async def authenticate(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        public = request.method in {"GET", "HEAD"} and request.path in _PUBLIC_PATHS
        if request.method == "OPTIONS" or public:
            return await handler(request)

        provided = ""
        authorization = request.headers.getall("Authorization", [])
        if authorization:
            # Never fall back to a query token after a malformed/bad header.
            if len(authorization) == 1:
                scheme, separator, value = authorization[0].partition(" ")
                if separator and scheme.lower() == "bearer":
                    provided = value
        elif request.method in {"GET", "HEAD"} and (
            request.path == "/ws"
            or re.fullmatch(r"/api/projects/[^/]+/artifacts", request.path)
        ):
            values = request.query.getall("access_token", [])
            if len(values) == 1:
                provided = values[0]

        if not hmac.compare_digest(provided.encode("utf-8"), token.encode("ascii")):
            return web.json_response(
                {"error": "unauthorized"}, status=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )

        response = await handler(request)
        if not response.prepared:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return authenticate
