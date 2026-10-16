"""The Mira Server HTTP application: ``/server/*``, the ``/api/*`` proxy and ``/ws``.

Client contract (protocol 1): MIRA-Intelligence/mira#152 (decisions comment)
and ``docs/server-edition/mira-server.md``. All errors are
``{"error": "<code>", "message": "..."}``.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import aiohttp
from aiohttp import web
from yarl import URL

from mira_server import PRODUCT, PROTOCOL, server_version
from mira_server.auth.backends import PasswordBackend
from mira_server.auth.identity import (
    AccessDeniedError,
    Identity,
    IdentityResolver,
    UnknownUserError,
)
from mira_server.auth.ratelimit import LoginRateLimiter
from mira_server.auth.sessions import RefreshError, SessionService
from mira_server.config import DEV_ORIGINS, ServerConfig, valid_username
from mira_server.engines import (
    CONFLICT,
    ERROR,
    RUNNING,
    WS_CLOSE_ENGINE_RESTART,
    EngineManager,
    UserEngine,
)
from mira_server.proxy import (
    HOP_BY_HOP,
    REQUEST_ID_HEADER,
    ROLE_HEADER,
    USER_HEADER,
    forward_request_headers,
    forward_response_headers,
    rewrite_client_frame,
    strip_token_query,
)
from mira_server.spawner.base import EngineHandle
from mira_server.store import Store
from mira_server.team import TEAM_FILENAME, TeamConfig, TeamConfigError

log = logging.getLogger("mira_server")

RETRY_AFTER_STARTING = 2
START_GRACE_S = 1.5
SWEEP_INTERVAL_S = 30.0
PURGE_INTERVAL_S = 600.0
MAX_WS_MESSAGE = 64 * 1024 * 1024
_MAX_LOGIN_BODY = 16 * 1024
_MAX_CONFIG_BODY = 1024 * 1024
# LLM requests are sent with a Content-Length (some gateways refuse chunked
# request bodies), so they are read whole; images make them large.
_MAX_LLM_BODY = 64 * 1024 * 1024
TEAM_PUSH_TIMEOUT_S = 10.0
# Engine API paths Mira Server answers itself for administrators (the team layer).
_TEAM_CONFIG_PATH = "/api/config"
_TEAM_PROVIDER_PREFIX = "/api/providers/"
# Engine API only Mira Server may call (it pushes the team layer there).
_ENGINE_TEAM_PREFIX = "/api/team"
# Headers that carry a provider API key, by the clients engines use.
_CREDENTIAL_HEADERS = ("authorization", "x-api-key", "api-key", "x-goog-api-key")

STATE_KEY: web.AppKey["MiraServer"] = web.AppKey("mira_server", object)  # type: ignore[arg-type]
# Request-scoped values (typed keys on newer aiohttp, plain strings on older ones).
_RequestKey = getattr(web, "RequestKey", None)
REQUEST_ID = _RequestKey("request_id", str) if _RequestKey else "request_id"
REQUEST_USER = _RequestKey("mira_user", str) if _RequestKey else "mira_user"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def json_error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status, headers=headers)


def _unauthorized(message: str = "missing, invalid or expired token") -> ApiError:
    return ApiError(401, "unauthorized", message, {"WWW-Authenticate": "Bearer"})


_HTTP_ERROR_CODES = {
    400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "payload_too_large",
}


@dataclass(frozen=True)
class AuthContext:
    session_id: str
    identity: Identity


class MiraServer:
    def __init__(
        self,
        config: ServerConfig,
        *,
        store: Store,
        backend: PasswordBackend,
        resolver: IdentityResolver,
        engines: EngineManager,
        clock: Callable[[], float] = time.monotonic,
        sweep_interval: float = SWEEP_INTERVAL_S,
        start_grace: float = START_GRACE_S,
        dev: bool = False,
        team: TeamConfig | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.backend = backend
        self.resolver = resolver
        self.engines = engines
        self.team = team or TeamConfig(config.server.data_dir / TEAM_FILENAME)
        self.llm_proxy_base = f"{_local_url(config.server.host, config.server.port)}/llm"
        engines.on_ready = self.push_team_layer
        self.sessions = SessionService(store, access_ttl=config.auth.access_token_ttl,
                                       refresh_ttl=config.auth.refresh_token_ttl)
        self.limiter = LoginRateLimiter(config.auth.login_max_attempts, config.auth.login_window)
        self.origins = config.server.effective_origins()
        if dev:
            # `serve --dev`: also the mira-ui Vite dev server.
            self.origins += [origin for origin in DEV_ORIGINS if origin not in self.origins]
        self.trusted_proxies = set(config.server.trusted_proxies)
        self.sweep_interval = sweep_interval
        self.start_grace = start_grace
        self._clock = clock
        self._identities: dict[str, tuple[Identity, float]] = {}
        self._nss = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mira-nss")
        self._background: list[asyncio.Task] = []
        self.http: aiohttp.ClientSession | None = None

    # ── identity and groups ──────────────────────────────────────────

    async def _resolve(self, username: str) -> Identity:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._nss, self.resolver.resolve, username)

    async def resolve_or_none(self, username: str) -> Identity | None:
        try:
            return await self._resolve(username)
        except (UnknownUserError, AccessDeniedError):
            return None

    async def current_identity(self, username: str, *, force: bool = False) -> Identity:
        """The user's identity, re-reading groups at most every ``group_recheck_interval``.

        A user who lost access has every session revoked and their engine stopped.
        """
        cached = self._identities.get(username)
        now = self._clock()
        if cached is not None and not force and now - cached[1] < self.config.auth.group_recheck_interval:
            return cached[0]
        try:
            identity = await self._resolve(username)
        except (UnknownUserError, AccessDeniedError) as exc:
            await self.revoke_access(username, str(exc) or "account removed")
            raise ApiError(403, "forbidden", "your account may no longer use Mira Server") from None
        except Exception as exc:  # noqa: BLE001 - NSS/directory trouble is transient, never a revocation
            log.warning("cannot look up %s: %s: %s", username, type(exc).__name__, exc)
            raise ApiError(503, "unavailable", "account lookup failed; try again shortly",
                           {"Retry-After": "5"}) from None
        self._identities[username] = (identity, now)
        if not identity.is_user and self.engines.get(username).state != "stopped":
            # Still an administrator, but no longer in users_group: no engine.
            log.warning("%s left %s; stopping their engine", username, self.config.auth.users_group)
            self.store.audit("engine.revoked", username=username, reason="not in users group")
            self.engines.background(self.engines.stop_or_forget(username, reason="access_revoked"))
        return identity

    async def revoke_access(self, username: str, reason: str) -> None:
        self._identities.pop(username, None)
        count = self.sessions.revoke_user(username, "access_revoked")
        log.warning("access revoked for %s: %s", username, reason)
        self.store.audit("access.revoked", username=username, sessions=count, reason=reason)
        self.engines.background(self.engines.stop_or_forget(username, reason="access_revoked"))

    # ── request helpers ──────────────────────────────────────────────

    def client_ip(self, request: web.Request) -> str:
        peer = request.remote or "?"
        if peer in self.trusted_proxies:
            forwarded = request.headers.get("X-Forwarded-For", "")
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            if hops:
                return hops[-1]
        return peer

    @staticmethod
    def _bearer(request: web.Request, *, allow_query: bool) -> str | None:
        """Token from ``Authorization: Bearer``, or ``?access_token=`` where URLs cannot carry headers."""
        scheme, _, value = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        if allow_query:
            token = request.query.get("access_token")
            if token:
                return token
        return None

    async def authenticate(self, request: web.Request, *, allow_query: bool = False,
                           force_recheck: bool = False) -> AuthContext:
        grant = self.sessions.check_access(self._bearer(request, allow_query=allow_query))
        if grant is None:
            raise _unauthorized()
        identity = await self.current_identity(grant.username, force=force_recheck)
        request[REQUEST_USER] = identity.name
        return AuthContext(grant.session_id, identity)

    @staticmethod
    async def _json_body(request: web.Request, *, lenient: bool = False) -> dict[str, Any]:
        if request.content_length is not None and request.content_length > _MAX_LOGIN_BODY:
            raise ApiError(413, "payload_too_large", "request body is too large")
        raw = await request.content.read(_MAX_LOGIN_BODY + 1)
        if len(raw) > _MAX_LOGIN_BODY:
            raise ApiError(413, "payload_too_large", "request body is too large")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            if lenient:
                return {}
            raise ApiError(400, "bad_request", "body must be JSON") from None
        if not isinstance(body, dict):
            if lenient:
                return {}
            raise ApiError(400, "bad_request", "body must be a JSON object")
        return body

    def _token_response(self, identity: Identity, tokens: Any) -> web.Response:
        return web.json_response({**tokens.as_json(), "user": identity.as_json()})

    # ── /server/* ────────────────────────────────────────────────────

    async def handle_info(self, request: web.Request) -> web.Response:
        return web.json_response({
            "product": PRODUCT,
            "version": server_version(),
            "protocol": PROTOCOL,
            "auth": {"methods": ["password"]},
        })

    async def handle_login(self, request: web.Request) -> web.Response:
        body = await self._json_body(request)
        username, password, client = body.get("username"), body.get("password"), body.get("client")
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            raise ApiError(400, "bad_request", "username and password are required")
        client = client[:64] if isinstance(client, str) else None
        ip = self.client_ip(request)
        key = self.limiter.key(username, ip)
        wait = self.limiter.retry_after(key)
        if wait > 0:
            self.store.audit("login.rate_limited", username=username[:64], client_ip=ip)
            raise ApiError(429, "too_many_attempts", "too many failed sign-in attempts; try again later",
                           {"Retry-After": str(max(1, math.ceil(wait)))})
        ok = valid_username(username) and len(password) <= 4096 and await self.backend.verify(username, password)
        if not ok:
            self.limiter.record_failure(key)
            self.store.audit("login.failed", username=username[:64], client_ip=ip)
            raise ApiError(401, "unauthorized", "invalid username or password")
        try:
            identity = await self._resolve(username)
        except (UnknownUserError, AccessDeniedError) as exc:
            self.store.audit("login.denied", username=username, client_ip=ip, reason=str(exc))
            raise ApiError(403, "forbidden",
                           f"your account is not allowed to use Mira Server (groups {self.config.auth.users_group} "
                           f"and {self.config.auth.admins_group})") from None
        self.limiter.reset(key)
        request[REQUEST_USER] = identity.name
        self._identities[identity.name] = (identity, self._clock())
        self.store.upsert_user(identity.name, uid=identity.uid, display_name=identity.display_name,
                               roles=list(identity.roles), login=True)
        session_id, tokens = self.sessions.login(identity.name, client)
        self.store.audit("login.ok", username=identity.name, client_ip=ip, session=session_id[:8], client=client)
        return self._token_response(identity, tokens)

    def _refresh_rejected(self, request: web.Request, exc: RefreshError) -> ApiError:
        if exc.reason == "reused":
            log.warning("security: refresh token reuse for %s; session revoked", exc.username)
            self.store.audit("security.refresh_reuse", username=exc.username,
                             client_ip=self.client_ip(request), session=(exc.session_id or "")[:8])
        return _unauthorized("invalid or expired refresh token")

    async def handle_refresh(self, request: web.Request) -> web.Response:
        """400/401/403 only for definitive token or account problems; transient failures are 5xx.

        The client signs out on 400/401/403 and keeps its tokens otherwise, so the
        refresh token is consumed only after the account check succeeded.
        """
        body = await self._json_body(request)
        token = body.get("refresh_token")
        token = token if isinstance(token, str) else None
        try:
            grant = self.sessions.check_refresh(token)
        except RefreshError as exc:
            raise self._refresh_rejected(request, exc) from None
        request[REQUEST_USER] = grant.username
        identity = await self.current_identity(grant.username, force=True)  # 403 or 503
        try:
            grant, tokens = self.sessions.refresh(token)
        except RefreshError as exc:
            raise self._refresh_rejected(request, exc) from None
        return self._token_response(identity, tokens)

    async def handle_logout(self, request: web.Request) -> web.Response:
        """Revoke the caller's session; ``{"refresh_token"}`` also works without a valid access token."""
        grant = self.sessions.check_access(self._bearer(request, allow_query=False))
        body = await self._json_body(request, lenient=True)
        refresh = body.get("refresh_token")
        revoked = None
        if grant is not None:
            self.sessions.revoke(grant.session_id, "logout")
            revoked = grant
        if isinstance(refresh, str):
            revoked = self.sessions.revoke_by_refresh(refresh, "logout") or revoked
        if revoked is None:
            raise _unauthorized()
        request[REQUEST_USER] = revoked.username
        self.store.audit("logout", username=revoked.username, client_ip=self.client_ip(request))
        return web.Response(status=204)

    async def handle_me(self, request: web.Request) -> web.Response:
        ctx = await self.authenticate(request)
        return web.json_response({"user": ctx.identity.as_json(),
                                  "engine": self.engines.snapshot(ctx.identity.name)})

    async def handle_engine_start(self, request: web.Request) -> web.Response:
        ctx = await self.authenticate(request)
        self._require_engine_role(ctx.identity)
        engine = await self.engines.ensure(ctx.identity, wait=self.start_grace, force=True)
        return web.json_response(engine.public())

    async def handle_engine_stop(self, request: web.Request) -> web.Response:
        ctx = await self.authenticate(request)
        engine = await self.engines.stop(ctx.identity, reason="user")
        return web.json_response(engine.public())

    # ── engine availability ──────────────────────────────────────────

    def _require_engine_role(self, identity: Identity) -> None:
        if not identity.is_user:
            raise ApiError(403, "no_engine",
                           f"this account is not in {self.config.auth.users_group}: it can change the team "
                           "settings but has no engine")

    async def _ready_engine(self, identity: Identity) -> UserEngine:
        self._require_engine_role(identity)
        engine = await self.engines.ensure(identity, wait=self.start_grace)
        if engine.state == RUNNING and engine.handle is not None:
            return engine
        if engine.state == CONFLICT:
            raise ApiError(409, "engine_conflict", engine.message or "another engine holds your engine lease")
        if engine.state == ERROR:
            raise ApiError(503, "engine_unavailable", engine.message or "your engine failed to start")
        raise ApiError(503, "engine_starting", "your engine is starting",
                       {"Retry-After": str(RETRY_AFTER_STARTING)})

    def _engine_gone(self, engine: UserEngine, handle: Any, identity: Identity) -> ApiError:
        self.engines.mark_unreachable(engine, handle)
        self.engines.request_start(identity)
        return ApiError(503, "engine_starting", "your engine is restarting",
                        {"Retry-After": str(RETRY_AFTER_STARTING)})

    # ── /api/* ───────────────────────────────────────────────────────

    async def handle_api(self, request: web.Request) -> web.StreamResponse:
        # ?access_token= is for <img>, <iframe> and download URLs, which cannot set headers.
        ctx = await self.authenticate(request, allow_query=request.method in {"GET", "HEAD"})
        identity = ctx.identity
        path = request.rel_url.path
        if path == _ENGINE_TEAM_PREFIX or path.startswith(_ENGINE_TEAM_PREFIX + "/"):
            raise ApiError(404, "not_found", "not found")
        if identity.is_admin:
            team_response = await self._team_api(request, identity, path)
            if team_response is not None:
                return team_response
        engine = await self._ready_engine(identity)
        handle = engine.handle
        assert handle is not None and self.http is not None
        query = strip_token_query(request.rel_url.raw_query_string)
        target = URL.build(scheme="http", host="127.0.0.1", port=handle.port, path=request.rel_url.raw_path,
                           query_string=query, encoded=True)
        headers = forward_request_headers(request.headers, engine_token=handle.token, user=identity.name,
                                          role=identity.role, request_id=request[REQUEST_ID])
        data = request.content.iter_any() if request.body_exists else None
        self.engines.begin_request(engine)
        try:
            try:
                upstream = await self.http.request(
                    request.method, target, headers=headers, data=data, allow_redirects=False,
                    skip_auto_headers=("Accept-Encoding", "User-Agent", "Content-Type"),
                    timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10),
                )
            except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError):
                raise self._engine_gone(engine, handle, identity) from None
            async with upstream:
                if upstream.status == 401:
                    # The engine no longer accepts the token we hold (restarted with a new one).
                    raise self._engine_gone(engine, handle, identity)
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                              headers=forward_response_headers(upstream.headers))
                await response.prepare(request)
                try:
                    async for chunk in upstream.content.iter_any():
                        await response.write(chunk)
                except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError) as exc:
                    # Headers are out; all we can do is cut the client connection short.
                    log.info("engine response for %s ended early: %s", identity.name, type(exc).__name__)
                    response.force_close()
                    return response
                await response.write_eof()
                return response
        finally:
            self.engines.end_request(engine)

    # ── team settings (administrators) ───────────────────────────────

    async def _team_api(self, request: web.Request, identity: Identity, path: str) -> web.Response | None:
        """Answer an administrator's config request from the team layer; ``None`` passes it on.

        Responses use the engine's error shape (``{"error": message}``), since
        the app talks to these endpoints as it would to an engine.
        """
        method = request.method
        if path == _TEAM_CONFIG_PATH and method == "GET":
            return web.json_response(self.team.payload())
        if path == _TEAM_CONFIG_PATH and method == "POST":
            return await self._team_update(request, identity)
        if path.startswith(_TEAM_PROVIDER_PREFIX):
            name, _, action = path[len(_TEAM_PROVIDER_PREFIX):].partition("/")
            if action == "models" and method == "GET":
                return await self._team_provider_models(request, name)
            if action == "test" and method == "POST":
                return await self._team_provider_test(request, name)
        return None

    async def _config_body(self, request: web.Request) -> Any:
        raw = await request.content.read(_MAX_CONFIG_BODY + 1)
        if len(raw) > _MAX_CONFIG_BODY:
            raise ApiError(413, "payload_too_large", "request body is too large")
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return None

    async def _team_update(self, request: web.Request, identity: Identity) -> web.Response:
        body = await self._config_body(request)
        if not isinstance(body, dict):
            return web.json_response({"error": "config payload must be an object"}, status=400)
        try:
            changed, paths = self.team.update(body)
        except TeamConfigError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OSError as exc:
            log.error("cannot save the team configuration %s: %s", self.team.path, exc)
            return web.json_response({"error": f"failed to save the team configuration: {exc}"}, status=500)
        if changed:
            self.store.audit("team.config_changed", username=identity.name,
                             client_ip=self.client_ip(request), settings=paths)
            log.info("team settings changed by %s: %s", identity.name, ", ".join(paths))
            self.engines.background(self.push_team_layer_all())
        return web.json_response(self.team.payload(persisted=changed))

    async def _team_provider_models(self, request: web.Request, name: str) -> web.Response:
        from mira_engine.providers.model_fetch import (
            ModelFetchError,
            fetch_models_to_cache,
            is_cache_stale,
            read_model_cache,
        )
        from mira_engine.providers.registry import find_by_name

        spec = find_by_name(name) if name else None
        if spec is None:
            return web.json_response({"error": f"unknown provider: {name}"}, status=400)
        if request.query.get("refresh", "").lower() not in ("1", "true", "yes"):
            cache = read_model_cache(spec.name, self.team.path)
            if cache is not None and not is_cache_stale(cache):
                return web.json_response(_models_payload(cache, cached=True))
        try:
            cache, _ = await fetch_models_to_cache(self.team.engine_config(), spec.name, self.team.path)
        except ModelFetchError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        except Exception as exc:  # noqa: BLE001 - surface as actionable error
            log.warning("team model fetch for %s failed: %s", spec.name, type(exc).__name__)
            return web.json_response({"error": f"model fetch failed: {exc}"}, status=502)
        return web.json_response(_models_payload(cache, cached=False))

    async def _team_provider_test(self, request: web.Request, name: str) -> web.Response:
        from mira_engine.providers.model_fetch import ModelFetchError, fetch_provider_models
        from mira_engine.providers.registry import find_by_name

        spec = find_by_name(name) if name else None
        if spec is None:
            return web.json_response({"error": f"unknown provider: {name}"}, status=400)
        body = await self._config_body(request)
        body = body if isinstance(body, dict) else {}
        config = self.team.engine_config()
        provider_cfg = getattr(config.providers, spec.name, None)
        if provider_cfg is None:
            return web.json_response({"error": f"unknown provider: {name}"}, status=400)
        # Transient credentials for this test only, never saved.
        api_key = body.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            provider_cfg.api_key = api_key.strip()
        if "api_base" in body:
            api_base = body["api_base"]
            provider_cfg.api_base = None if api_base in (None, "") else str(api_base).strip()
        try:
            cache = await fetch_provider_models(config, spec.name)
        except ModelFetchError as exc:
            return web.json_response({"ok": False, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report failure to the app
            return web.json_response({"ok": False, "message": f"connection failed: {exc}"})
        return web.json_response({"ok": True, "message": "Connection succeeded", "model_count": len(cache.models)})

    async def push_team_layer(self, name: str, handle: EngineHandle) -> None:
        """Give one engine the current team layer (on start, attach and every change)."""
        if self.http is None:
            return
        layer = self.team.layer_for(handle.token, self.llm_proxy_base)
        url = f"http://127.0.0.1:{handle.port}{_ENGINE_TEAM_PREFIX}/layer"
        try:
            async with self.http.put(url, json={"layer": layer},
                                     headers={"Authorization": f"Bearer {handle.token}"},
                                     timeout=aiohttp.ClientTimeout(total=TEAM_PUSH_TIMEOUT_S)) as resp:
                if resp.status == 404:
                    if layer["providers"] or layer.get("defaults") or "model_params" in layer:
                        log.warning("the engine of %s is too old for team settings; upgrade mira-engine", name)
                        self.store.audit("team.push_unsupported", username=name)
                    return
                if resp.status != 200:
                    log.warning("the engine of %s refused the team settings (HTTP %s)", name, resp.status)
                    self.store.audit("team.push_failed", username=name, status=resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("cannot give the engine of %s the team settings: %s", name, type(exc).__name__)
            self.store.audit("team.push_failed", username=name, error=type(exc).__name__)

    async def push_team_layer_all(self) -> None:
        engines = [(e.name, e.handle) for e in self.engines.running() if e.handle is not None]
        await asyncio.gather(*(self.push_team_layer(name, handle) for name, handle in engines))

    # ── /llm/<provider>/* (engines → team providers) ─────────────────

    def _local_engine_request(self, request: web.Request) -> bool:
        """Only engines on this host, never a client through the TLS proxy."""
        peer = request.remote or ""
        if peer in self.trusted_proxies or "X-Forwarded-For" in request.headers or "Forwarded" in request.headers:
            return False
        try:
            return ipaddress.ip_address(peer).is_loopback
        except ValueError:
            return False

    def _engine_for_token(self, presented: str) -> UserEngine | None:
        for engine in self.engines.running():
            handle = engine.handle
            if handle is not None and hmac.compare_digest(handle.token.encode(), presented.encode()):
                return engine
        return None

    def _llm_credential(self, request: web.Request) -> tuple[str, str, str] | None:
        """(header name or "?key", prefix, token) of the credential the engine sent."""
        for header in _CREDENTIAL_HEADERS:
            value = request.headers.get(header, "").strip()
            if not value:
                continue
            prefix = ""
            if header == "authorization":
                scheme, _, token = value.partition(" ")
                if scheme.lower() != "bearer" or not token.strip():
                    continue
                prefix, value = f"{scheme} ", token.strip()
            return header, prefix, value
        key = request.query.get("key", "").strip()
        return ("?key", "", key) if key else None

    async def handle_llm(self, request: web.Request) -> web.StreamResponse:
        if not self._local_engine_request(request):
            raise ApiError(403, "forbidden", "the LLM proxy only serves engines on this host")
        credential = self._llm_credential(request)
        engine = self._engine_for_token(credential[2]) if credential else None
        if engine is None or engine.identity is None:
            raise _unauthorized("not a running engine's token")
        identity = await self.current_identity(engine.name)
        request[REQUEST_USER] = identity.name
        self._require_engine_role(identity)
        provider = request.match_info["provider"]
        upstream = self.team.upstream(provider)
        if upstream is None:
            raise ApiError(404, "not_found", f"{provider} is not a team provider")
        assert credential is not None and self.http is not None
        where, prefix, _ = credential

        tail = request.rel_url.raw_path[len(f"/llm/{provider}"):]
        query = request.rel_url.raw_query_string
        if where == "?key":
            kept = [part for part in query.split("&") if part and part.split("=", 1)[0] != "key"]
            query = "&".join([*kept, f"key={quote(upstream.api_key, safe='')}"])
        target = URL(upstream.base + tail + (f"?{query}" if query else ""), encoded=True)

        drop = HOP_BY_HOP | {"host", "cookie", "origin", "content-length"}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        if where != "?key":
            for key in [k for k in headers if k.lower() == where]:
                headers[key] = f"{prefix}{upstream.api_key}"
        data = None
        if request.body_exists:
            chunks, size = [], 0
            async for chunk in request.content.iter_any():
                size += len(chunk)
                if size > _MAX_LLM_BODY:
                    raise ApiError(413, "payload_too_large", "request body is too large")
                chunks.append(chunk)
            data = b"".join(chunks)
        self.engines.begin_request(engine)
        try:
            try:
                upstream_resp = await self.http.request(
                    request.method, target, headers=headers, data=data, allow_redirects=False,
                    proxy=self.team.proxy(), skip_auto_headers=("Accept-Encoding", "User-Agent", "Content-Type"),
                    timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30),
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("LLM proxy to %s for %s failed: %s", provider, identity.name, type(exc).__name__)
                raise ApiError(502, "bad_gateway", f"cannot reach {provider}: {type(exc).__name__}") from None
            async with upstream_resp:
                response = web.StreamResponse(status=upstream_resp.status, reason=upstream_resp.reason,
                                              headers=forward_response_headers(upstream_resp.headers))
                await response.prepare(request)
                try:
                    async for chunk in upstream_resp.content.iter_any():
                        await response.write(chunk)
                except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError):
                    response.force_close()
                    return response
                await response.write_eof()
                return response
        finally:
            self.engines.end_request(engine)

    # ── /ws ──────────────────────────────────────────────────────────

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        """``/ws`` and ``/ws/*`` (e.g. ``/ws/workflow/v1``) to the same path on the user's engine.

        ``user_id`` is overwritten in JSON-object client frames on every path; the
        workflow socket reads only its own keys, so the extra field is harmless there.
        A stopped engine is started; if it is not ready within the start grace the
        upgrade is refused with 503 ``engine_starting``.
        """
        ctx = await self.authenticate(request, allow_query=True)
        identity = ctx.identity
        engine = await self._ready_engine(identity)
        handle = engine.handle
        assert handle is not None and self.http is not None
        # The same path on the engine (/ws, /ws/workflow/v1, ...); only the engine token in the query.
        url = URL.build(scheme="http", host="127.0.0.1", port=handle.port, path=request.rel_url.raw_path,
                        query_string=f"access_token={quote(handle.token, safe='')}", encoded=True)
        try:
            upstream = await self.http.ws_connect(
                url,
                headers={USER_HEADER: identity.name, ROLE_HEADER: identity.role,
                         REQUEST_ID_HEADER: request[REQUEST_ID]},
                autoping=True,
                max_msg_size=MAX_WS_MESSAGE,
                timeout=aiohttp.ClientWSTimeout(ws_close=5.0),
            )
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status == 401:
                raise self._engine_gone(engine, handle, identity) from None
            raise ApiError(503, "engine_unavailable", f"the engine refused the WebSocket ({exc.status})") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise self._engine_gone(engine, handle, identity) from None

        client = web.WebSocketResponse(heartbeat=30.0, max_msg_size=MAX_WS_MESSAGE)
        try:
            await client.prepare(request)
        except Exception:
            await upstream.close()
            raise
        self.engines.ws_opened(engine, client)
        try:
            await self._pump(client, upstream, identity.name)
        finally:
            self.engines.ws_closed(engine, client)
            if not upstream.closed:
                await upstream.close()
        return client

    async def _pump(self, client: web.WebSocketResponse, upstream: aiohttp.ClientWebSocketResponse,
                    username: str) -> None:
        async def client_to_engine() -> None:
            async for msg in client:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await upstream.send_str(rewrite_client_frame(msg.data, username))
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await upstream.send_bytes(msg.data)
            await upstream.close()

        async def engine_to_client() -> None:
            async for msg in upstream:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await client.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await client.send_bytes(msg.data)
            if not client.closed:
                # The engine went away (restart, crash, stop elsewhere); the client reconnects.
                await client.close(code=WS_CLOSE_ENGINE_RESTART, message=b"engine restarting")

        tasks = [asyncio.create_task(client_to_engine()), asyncio.create_task(engine_to_client())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()

    # ── middleware, CORS, background work ────────────────────────────

    def origin_allowed(self, origin: str | None) -> bool:
        return origin is None or origin in self.origins

    @web.middleware
    async def middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        request_id = uuid.uuid4().hex
        request[REQUEST_ID] = request_id
        started = time.monotonic()
        response: web.StreamResponse
        try:
            origin = request.headers.get("Origin")
            if not self.origin_allowed(origin):
                raise ApiError(403, "origin_not_allowed", "this origin may not use Mira Server")
            if request.method == "OPTIONS":
                response = self._preflight(request)
            else:
                response = await handler(request)
        except ApiError as exc:
            response = json_error(exc.status, exc.code, exc.message, exc.headers)
        except web.HTTPException as exc:
            code = _HTTP_ERROR_CODES.get(exc.status, "error")
            response = json_error(exc.status, code, exc.reason or code, None)
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception:  # noqa: BLE001 - always answer with the JSON error shape
            log.exception("unhandled error on %s %s (request %s)", request.method, request.path, request_id)
            response = json_error(500, "internal", "internal server error")
        # Paths only: query strings may carry tokens.
        log.info("%s %s %s user=%s %.0fms", request.method, request.path, response.status,
                 request.get(REQUEST_USER, "-"), (time.monotonic() - started) * 1000)
        return response

    def _preflight(self, request: web.Request) -> web.Response:
        response = web.Response(status=204)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        requested = request.headers.get("Access-Control-Request-Headers", "")
        allowed = [h.strip() for h in requested.split(",") if h.strip() and h.strip().replace("-", "").isalnum()]
        response.headers["Access-Control-Allow-Headers"] = ", ".join(
            dict.fromkeys(["Authorization", "Content-Type", REQUEST_ID_HEADER, *allowed]))
        response.headers["Access-Control-Max-Age"] = "600"
        return response

    async def on_prepare(self, request: web.Request, response: web.StreamResponse) -> None:
        request_id = request.get(REQUEST_ID)
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        origin = request.headers.get("Origin")
        if origin and origin in self.origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Expose-Headers"] = f"{REQUEST_ID_HEADER}, Retry-After, Content-Disposition"
            vary = response.headers.get("Vary")
            response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"

    async def sweep_once(self) -> None:
        for engine in list(self.engines.running()):
            try:
                await self.current_identity(engine.name)
            except ApiError:
                pass  # current_identity revoked access and stopped the engine
        await self.engines.health_sweep()
        await self.engines.cull_idle()

    async def _sweeper(self) -> None:
        last_purge = 0.0
        while True:
            await asyncio.sleep(self.sweep_interval)
            try:
                await self.sweep_once()
                if time.monotonic() - last_purge > PURGE_INTERVAL_S:
                    self.store.purge_expired()
                    last_purge = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep sweeping
                log.exception("background sweep failed")

    async def on_startup(self, app: web.Application) -> None:
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                                          connector=aiohttp.TCPConnector(limit=0))
        self.engines.http = self.http
        self._background.append(asyncio.create_task(self.engines.reattach_all(self.resolve_or_none)))
        self._background.append(asyncio.create_task(self._sweeper()))

    async def on_cleanup(self, app: web.Application) -> None:
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.engines.close()
        if self.http is not None:
            await self.http.close()
        self._nss.shutdown(wait=False)

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware])
        app[STATE_KEY] = self
        app.on_response_prepare.append(self.on_prepare)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        app.router.add_get("/server/info", self.handle_info)
        app.router.add_post("/server/auth/login", self.handle_login)
        app.router.add_post("/server/auth/refresh", self.handle_refresh)
        app.router.add_post("/server/auth/logout", self.handle_logout)
        app.router.add_get("/server/me", self.handle_me)
        app.router.add_post("/server/engine/start", self.handle_engine_start)
        app.router.add_post("/server/engine/stop", self.handle_engine_stop)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/ws/{tail:.+}", self.handle_ws)
        app.router.add_route("*", "/api", self.handle_api)
        app.router.add_route("*", "/api/{tail:.*}", self.handle_api)
        app.router.add_route("*", "/llm/{provider}", self.handle_llm)
        app.router.add_route("*", "/llm/{provider}/{tail:.*}", self.handle_llm)
        return app


def _local_url(host: str, port: int) -> str:
    """How engines on this host reach Mira Server."""
    if host in {"", "0.0.0.0"}:
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _models_payload(cache: Any, *, cached: bool) -> dict[str, Any]:
    return {
        "provider": cache.provider,
        "api_base": cache.api_base,
        "fetched_at": cache.fetched_at,
        "models": [model.config_model for model in cache.models],
        "model_details": [
            {"id": model.id, "config_model": model.config_model, "display_name": model.display_name,
             "context_window_tokens": model.context_window_tokens}
            for model in cache.models
        ],
        "cached": cached,
    }


def create_server(config: ServerConfig, **overrides: Any) -> MiraServer:
    """Wire the production components from ``config``; ``overrides`` replace any of them."""
    from mira_server.auth.backends import make_backend
    from mira_server.auth.identity import DevIdentityResolver, NssIdentityResolver
    from mira_server.spawner import make_spawner

    store = overrides.pop("store", None) or Store(config.db_path)
    backend = overrides.pop("backend", None) or make_backend(config.auth)
    resolver = overrides.pop("resolver", None)
    if resolver is None:
        resolver = (DevIdentityResolver(config.auth) if config.auth.backend == "dev"
                    else NssIdentityResolver(config.auth.users_group, config.auth.admins_group))
    engines = overrides.pop("engines", None)
    if engines is None:
        spawner = overrides.pop("spawner", None) or make_spawner(config.spawner)
        engines = EngineManager(spawner, store, idle_timeout=config.spawner.idle_timeout,
                                verify_listener=config.spawner.verify_listener,
                                **overrides.pop("engine_options", {}))
    return MiraServer(config, store=store, backend=backend, resolver=resolver, engines=engines, **overrides)
