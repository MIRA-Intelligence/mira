"""The Mira Server HTTP application: ``/server/*``, the ``/api/*`` proxy and ``/ws``.

Client contract (protocol 1): MIRA-Intelligence/mira#152 (decisions comment)
and ``docs/server-edition/mira-server.md``. All errors are
``{"error": "<code>", "message": "..."}``.
"""

from __future__ import annotations

import asyncio
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
    REQUEST_ID_HEADER,
    ROLE_HEADER,
    USER_HEADER,
    forward_request_headers,
    forward_response_headers,
    rewrite_client_frame,
    strip_token_query,
)
from mira_server.store import Store

log = logging.getLogger("mira_server")

RETRY_AFTER_STARTING = 2
START_GRACE_S = 1.5
SWEEP_INTERVAL_S = 30.0
PURGE_INTERVAL_S = 600.0
MAX_WS_MESSAGE = 64 * 1024 * 1024
_MAX_LOGIN_BODY = 16 * 1024

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
    ) -> None:
        self.config = config
        self.store = store
        self.backend = backend
        self.resolver = resolver
        self.engines = engines
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
                           f"your account is not allowed to use Mira Server (group {self.config.auth.users_group})") \
                from None
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
        engine = await self.engines.ensure(ctx.identity, wait=self.start_grace, force=True)
        return web.json_response(engine.public())

    async def handle_engine_stop(self, request: web.Request) -> web.Response:
        ctx = await self.authenticate(request)
        engine = await self.engines.stop(ctx.identity, reason="user")
        return web.json_response(engine.public())

    # ── engine availability ──────────────────────────────────────────

    async def _ready_engine(self, identity: Identity) -> UserEngine:
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
        return app


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
