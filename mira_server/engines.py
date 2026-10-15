"""Per-user engine state: start (shared by concurrent callers), verify, stop, cull.

States, as reported to clients:

- ``stopped``: no engine known to Mira Server.
- ``starting``: a start or attach is in flight.
- ``running``: attached, listener verified, ``/health`` and ``/version`` answered.
- ``error``: the last start failed (retried on demand after a short backoff).
- ``conflict``: the engine lease is held by an engine Mira Server cannot attach
  to (another host, another token, a non-loopback bind).

Mira Server only culls engines it started itself; an engine it merely attached
to (for example a Remote-SSH one) is left running.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp

from mira_server.auth.identity import Identity
from mira_server.spawner.base import INTERNAL, EngineHandle, Spawner, SpawnerError
from mira_server.spawner.netcheck import ListenerCheck, check_listener
from mira_server.store import Store

log = logging.getLogger(__name__)

STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
ERROR = "error"
CONFLICT = "conflict"

ENGINE_SERVICE = "mira-gateway"
WS_CLOSE_ENGINE_STOPPED = 1001
WS_CLOSE_ENGINE_RESTART = 1012


class EngineProbeError(Exception):
    pass


@dataclass(eq=False)
class UserEngine:
    name: str
    state: str = STOPPED
    handle: EngineHandle | None = None
    version: str | None = None
    message: str | None = None
    identity: Identity | None = None
    started_by_server: bool = False
    failed_at: float | None = None
    last_activity: float = 0.0
    active_requests: int = 0
    websockets: set[Any] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task | None = None

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "version": self.version if self.state == RUNNING else None,
            "message": self.message,
        }


class EngineManager:
    def __init__(
        self,
        spawner: Spawner,
        store: Store,
        *,
        idle_timeout: float,
        verify_listener: bool = True,
        listener_check: Callable[..., ListenerCheck] = check_listener,
        retry_backoff: float = 10.0,
        probe_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spawner = spawner
        self.store = store
        self.idle_timeout = idle_timeout
        self.verify_listener = verify_listener
        self.listener_check = listener_check
        self.retry_backoff = retry_backoff
        self.probe_timeout = probe_timeout
        self._clock = clock
        self._engines: dict[str, UserEngine] = {}
        self._tasks: set[asyncio.Task] = set()
        self.http: aiohttp.ClientSession | None = None

    # ── lookup ───────────────────────────────────────────────────────

    def get(self, name: str) -> UserEngine:
        engine = self._engines.get(name)
        if engine is None:
            engine = self._engines[name] = UserEngine(name=name, last_activity=self._clock())
        return engine

    def snapshot(self, name: str) -> dict[str, Any]:
        return self.get(name).public()

    def running(self) -> list[UserEngine]:
        return [e for e in self._engines.values() if e.state == RUNNING and e.handle is not None]

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def background(self, coro: Any) -> asyncio.Task:
        """Run ``coro`` as a tracked task (cancelled on shutdown)."""
        return self._spawn(coro)

    async def drain(self) -> None:
        """Wait for tracked tasks (tests)."""
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.wait(pending)

    # ── start ────────────────────────────────────────────────────────

    def request_start(self, identity: Identity, *, force: bool = False) -> asyncio.Task | None:
        """Begin starting the user's engine unless it runs or a start is in flight.

        Concurrent callers share one start task. After a failure, new starts
        wait ``retry_backoff`` seconds unless ``force`` (an explicit start).
        """
        engine = self.get(identity.name)
        engine.identity = identity
        if engine.state == RUNNING and engine.handle is not None:
            return None
        if engine.task is not None and not engine.task.done():
            return engine.task
        if engine.state in (ERROR, CONFLICT) and not force and engine.failed_at is not None \
                and self._clock() - engine.failed_at < self.retry_backoff:
            return None
        engine.state = STARTING
        engine.message = None
        engine.task = self._spawn(self._start(engine, identity))
        return engine.task

    async def ensure(self, identity: Identity, *, wait: float = 0.0, force: bool = False) -> UserEngine:
        task = self.request_start(identity, force=force)
        if task is not None and wait > 0:
            await asyncio.wait({task}, timeout=wait)
        return self.get(identity.name)

    async def _start(self, engine: UserEngine, identity: Identity) -> None:
        async with engine.lock:
            if engine.state == RUNNING and engine.handle is not None:
                return
            engine.state = STARTING
            try:
                handle = await self.spawner.start(identity)
            except SpawnerError as exc:
                self._fail(engine, exc)
                return
            except Exception as exc:  # noqa: BLE001 - must always leave a final state
                log.exception("engine start for %s failed", identity.name)
                self._fail(engine, SpawnerError(INTERNAL, f"{type(exc).__name__}: {exc}"))
                return
            row = self.store.get_engine(identity.name)
            ours = handle.started or bool(row and row.started_by_server and row.pid == handle.pid)
            await self._adopt(engine, identity, handle, started_by_server=ours)

    def _fail(self, engine: UserEngine, exc: SpawnerError) -> None:
        engine.handle = None
        engine.state = CONFLICT if exc.conflict else ERROR
        engine.message = exc.message
        engine.failed_at = self._clock()
        log.warning("engine for %s: %s (%s)", engine.name, exc.message, exc.code)
        self.store.audit("engine.start_failed", username=engine.name, code=exc.code)

    def _set_error(self, engine: UserEngine, message: str) -> None:
        engine.handle = None
        engine.state = ERROR
        engine.message = message
        engine.failed_at = self._clock()

    async def _adopt(self, engine: UserEngine, identity: Identity, handle: EngineHandle, *,
                     started_by_server: bool) -> bool:
        """Verify the listener and health of a started or attached engine, then use it."""
        if self.verify_listener:
            expected = self.spawner.expected_uid(identity)
            result = self.listener_check(handle.port, expected, pid=handle.pid)
            if not result.ok:
                log.warning("security: refusing engine port %s for %s: %s", handle.port, identity.name,
                            result.reason)
                self.store.audit("security.listener_mismatch", username=identity.name, port=handle.port,
                                 expected_uid=expected, uids=list(result.uids), reason=result.reason)
                self._set_error(engine, "the engine port is not owned by your account; "
                                        "Mira Server refused to connect (security event logged)")
                return False
        try:
            version = await self._probe(handle.port)
        except EngineProbeError as exc:
            self._set_error(engine, f"the engine did not pass its health check: {exc}")
            self.store.audit("engine.unhealthy", username=identity.name, port=handle.port)
            return False
        engine.handle = handle
        engine.version = version or handle.version
        engine.state = RUNNING
        engine.message = None
        engine.failed_at = None
        engine.started_by_server = started_by_server
        engine.last_activity = self._clock()
        self.store.put_engine(identity.name, port=handle.port, pid=handle.pid, version=engine.version,
                              started_by_server=started_by_server)
        self.store.audit("engine.started" if handle.started else "engine.attached", username=identity.name,
                         port=handle.port, pid=handle.pid, version=engine.version)
        log.info("engine for %s %s on port %s (pid %s)", identity.name,
                 "started" if handle.started else "attached", handle.port, handle.pid)
        return True

    async def _probe(self, port: int) -> str | None:
        """``/health`` and ``/version`` must answer as a Mira gateway (both are open routes)."""
        if self.http is None:
            raise EngineProbeError("no HTTP client")
        timeout = aiohttp.ClientTimeout(total=self.probe_timeout)
        base = f"http://127.0.0.1:{port}"
        try:
            async with self.http.get(f"{base}/health", timeout=timeout) as resp:
                health = await resp.json(content_type=None) if resp.status == 200 else None
            async with self.http.get(f"{base}/version", timeout=timeout) as resp:
                version = await resp.json(content_type=None) if resp.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise EngineProbeError(f"{type(exc).__name__}") from None
        if not isinstance(health, dict) or health.get("service") != ENGINE_SERVICE:
            raise EngineProbeError("/health did not identify a Mira engine")
        if not isinstance(version, dict) or version.get("service") != ENGINE_SERVICE:
            raise EngineProbeError("/version did not identify a Mira engine")
        agent_version = version.get("agent_version")
        return agent_version if isinstance(agent_version, str) else None

    # ── stop ─────────────────────────────────────────────────────────

    async def stop(self, identity: Identity, *, reason: str = "user") -> UserEngine:
        engine = self.get(identity.name)
        engine.identity = identity
        await self.close_websockets(engine, WS_CLOSE_ENGINE_STOPPED, "engine stopped")
        if engine.task is not None and not engine.task.done():
            await asyncio.wait({engine.task})
        async with engine.lock:
            try:
                await self.spawner.stop(identity)
            except SpawnerError as exc:
                engine.state = CONFLICT if exc.conflict else ERROR
                engine.message = exc.message
                engine.failed_at = self._clock()
                self.store.audit("engine.stop_failed", username=identity.name, code=exc.code, reason=reason)
                return engine
            self._clear(engine)
            self.store.audit("engine.stopped", username=identity.name, reason=reason)
            log.info("engine for %s stopped (%s)", identity.name, reason)
        return engine

    def _clear(self, engine: UserEngine) -> None:
        engine.handle = None
        engine.state = STOPPED
        engine.version = None
        engine.message = None
        engine.failed_at = None
        engine.started_by_server = False
        self.store.delete_engine(engine.name)

    async def stop_or_forget(self, name: str, *, reason: str) -> None:
        """Stop the engine if possible; otherwise stop using it (access was revoked)."""
        engine = self.get(name)
        if engine.identity is not None and (engine.handle is not None or engine.state == STARTING):
            engine = await self.stop(engine.identity, reason=reason)
        if engine.state != STOPPED:
            await self.close_websockets(engine, WS_CLOSE_ENGINE_STOPPED, "access revoked")
            self._clear(engine)
            self.store.audit("engine.forgotten", username=name, reason=reason)

    def mark_unreachable(self, engine: UserEngine, handle: EngineHandle | None) -> None:
        """The engine stopped answering or rejected its token; re-attach on next use."""
        if engine.handle is not None and engine.handle is handle:
            log.info("engine for %s on port %s is gone or changed; will re-attach", engine.name, handle.port)
            engine.handle = None
            engine.state = STOPPED
            engine.version = None

    # ── re-attach after a Mira Server restart ───────────────────────

    async def reattach_all(self, resolve: Callable[[str], Any]) -> None:
        """Ask each previously known user's launcher for ``status`` instead of assuming."""
        for row in self.store.list_engines():
            identity = await resolve(row.username)
            if identity is None:
                self.store.delete_engine(row.username)
                continue
            engine = self.get(identity.name)
            engine.identity = identity
            async with engine.lock:
                if engine.state == RUNNING:
                    continue
                try:
                    handle = await self.spawner.status(identity)
                except SpawnerError as exc:
                    log.warning("re-attach for %s failed: %s", identity.name, exc.message)
                    continue
                if handle is None:
                    self.store.delete_engine(identity.name)
                    continue
                ours = row.started_by_server and row.pid == handle.pid
                await self._adopt(engine, identity, handle, started_by_server=ours)

    # ── activity and culling ─────────────────────────────────────────

    def begin_request(self, engine: UserEngine) -> None:
        engine.active_requests += 1
        engine.last_activity = self._clock()

    def end_request(self, engine: UserEngine) -> None:
        engine.active_requests = max(engine.active_requests - 1, 0)
        engine.last_activity = self._clock()

    def ws_opened(self, engine: UserEngine, ws: Any) -> None:
        engine.websockets.add(ws)
        engine.last_activity = self._clock()

    def ws_closed(self, engine: UserEngine, ws: Any) -> None:
        engine.websockets.discard(ws)
        engine.last_activity = self._clock()

    async def close_websockets(self, engine: UserEngine, code: int, message: str) -> None:
        for ws in list(engine.websockets):
            try:
                await ws.close(code=code, message=message.encode("utf-8"))
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    def idle_candidates(self) -> list[UserEngine]:
        if self.idle_timeout <= 0:
            return []
        now = self._clock()
        return [
            e for e in self.running()
            if e.started_by_server and not e.websockets and e.active_requests == 0
            and e.identity is not None and now - e.last_activity >= self.idle_timeout
        ]

    async def cull_idle(self) -> list[str]:
        culled = []
        for engine in self.idle_candidates():
            assert engine.identity is not None
            await self.stop(engine.identity, reason="idle")
            culled.append(engine.name)
        return culled

    async def health_sweep(self) -> None:
        for engine in self.running():
            handle = engine.handle
            try:
                await self._probe(handle.port)
            except EngineProbeError:
                self.mark_unreachable(engine, handle)
                await self.close_websockets(engine, WS_CLOSE_ENGINE_RESTART, "engine restarting")

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
