"""Runtime guards for plugin code (design §3.4/§3.6, mira#206/#207).

Every plugin tool and hook runs behind a :class:`CircuitBreaker`: calls are
bounded by a timeout, failures and timeouts are counted per plugin, and after
``threshold`` consecutive failures the plugin is *tripped* — persisted, so it
stays off across restarts until the user re-enables it. First-party
(core-bundled) plugins are only *paused* in memory for ``cooldown_s``: a web
search that keeps timing out shouldn't disable web search for good. A tripped
plugin's tools answer with an error the model can read; its hooks become
no-ops. The engine itself never sees a plugin exception.

All log lines carry the plugin id, so a misbehaving plugin is attributable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, TypeVar

from loguru import logger

from mira_engine.agent.hook import AgentHook, AgentHookContext
from mira_engine.agent.tools.base import Tool
from mira_engine.plugins.state import PluginState

T = TypeVar("T")

DEFAULT_TOOL_TIMEOUT_S = 60.0
DEFAULT_HOOK_TIMEOUT_S = 2.0
DEFAULT_TRIP_THRESHOLD = 3
DEFAULT_PAUSE_S = 300.0


class PluginTripped(Exception):
    """Raised when calling into a plugin whose breaker has tripped."""


class CircuitBreaker:
    def __init__(
        self,
        plugin_id: str,
        state: PluginState,
        threshold: int = DEFAULT_TRIP_THRESHOLD,
        version: str | None = None,
        *,
        persist: bool = True,
        cooldown_s: float = DEFAULT_PAUSE_S,
    ):
        self.plugin_id = plugin_id
        self.version = version
        self.state = state
        self.threshold = threshold
        self.consecutive_failures = 0
        self.persist = persist
        self.cooldown_s = cooldown_s
        self._paused_until = 0.0

    @property
    def paused(self) -> bool:
        return self._paused_until > time.monotonic()

    @property
    def tripped(self) -> bool:
        """Off because it kept failing, or because the platform disabled it."""
        return (
            self.paused
            or self.state.tripped(self.plugin_id) is not None
            or self.state.remote_reason(self.plugin_id, self.version) is not None
        )

    def success(self) -> None:
        self.consecutive_failures = 0

    def failure(self, what: str, error: BaseException | str) -> None:
        self.consecutive_failures += 1
        logger.warning(
            "plugin {} failed in {} ({}/{}): {}",
            self.plugin_id,
            what,
            self.consecutive_failures,
            self.threshold,
            error,
        )
        if self.consecutive_failures >= self.threshold and not self.tripped:
            reason = f"{self.consecutive_failures} consecutive failures, last in {what}: {error}"
            if not self.persist:
                self._paused_until = time.monotonic() + self.cooldown_s
                self.consecutive_failures = 0
                logger.error("plugin {} paused for {:.0f}s (circuit tripped): {}", self.plugin_id, self.cooldown_s, reason)
                return
            self.state.trip(self.plugin_id, reason)
            logger.error("plugin {} disabled (circuit tripped): {}", self.plugin_id, reason)

    async def call(self, what: str, aw: Awaitable[T], timeout: float | None) -> T:
        if self.tripped:
            close = getattr(aw, "close", None)
            if close:
                close()  # don't leak the never-awaited coroutine
            raise PluginTripped(self.plugin_id)
        try:
            result = await (asyncio.wait_for(aw, timeout) if timeout else aw)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.failure(what, f"timed out after {timeout}s")
            raise
        except Exception as e:
            self.failure(what, e)
            raise
        self.success()
        return result


class GuardedTool(Tool):
    """A plugin tool behind its plugin's breaker and a call timeout."""

    def __init__(self, inner: Tool, breaker: CircuitBreaker, timeout_s: float = DEFAULT_TOOL_TIMEOUT_S):
        self._inner = inner
        self._breaker = breaker
        self._timeout_s = timeout_s

    @property
    def plugin_id(self) -> str:
        return self._breaker.plugin_id

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._inner.parameters

    @property
    def read_only(self) -> bool:
        return self._inner.read_only

    @property
    def exclusive(self) -> bool:
        return self._inner.exclusive

    async def execute(self, **kwargs: Any) -> Any:
        try:
            return await self._breaker.call(f"tool {self.name}", self._inner.execute(**kwargs), self._timeout_s)
        except PluginTripped:
            if self._breaker.paused:
                return f"Error: tool '{self.name}' is paused after repeated failures; try again in a few minutes."
            return f"Error: tool '{self.name}' is unavailable — plugin '{self.plugin_id}' was disabled after repeated failures."
        except asyncio.TimeoutError:
            return f"Error: tool '{self.name}' (plugin '{self.plugin_id}') timed out after {self._timeout_s}s."
        except Exception as e:  # noqa: BLE001 - plugin errors become tool errors, never engine errors
            return f"Error: tool '{self.name}' (plugin '{self.plugin_id}') failed: {e}"


class GuardedHook(AgentHook):
    """A plugin hook behind its plugin's breaker. ``observe`` hooks receive a
    snapshot (via :class:`~mira_engine.agent.hook.CompositeHook`) and never
    take part in ``finalize_content``."""

    def __init__(
        self,
        inner: AgentHook,
        breaker: CircuitBreaker,
        mode: str = "observe",
        timeout_s: float = DEFAULT_HOOK_TIMEOUT_S,
    ):
        super().__init__(reraise=False)
        self._inner = inner
        self._breaker = breaker
        self.mode = "mutate" if mode == "mutate" else "observe"
        # The breaker enforces the timeout; CompositeHook must not double-wrap.
        self.timeout_s = None
        self._timeout = timeout_s

    def wants_streaming(self) -> bool:
        try:
            return not self._breaker.tripped and bool(self._inner.wants_streaming())
        except Exception:  # noqa: BLE001
            return False

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        try:
            await self._breaker.call(f"hook {method}", getattr(self._inner, method)(*args, **kwargs), self._timeout)
        except (PluginTripped, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001 - counted by the breaker, never raised
            pass

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._call("before_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._call("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._call("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._call("before_execute_tools", context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._call("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        if self.mode != "mutate" or self._breaker.tripped:
            return content
        try:
            result = self._inner.finalize_content(context, content)
        except Exception as e:  # noqa: BLE001
            self._breaker.failure("hook finalize_content", e)
            return content
        self._breaker.success()
        return result if result is None or isinstance(result, str) else content
