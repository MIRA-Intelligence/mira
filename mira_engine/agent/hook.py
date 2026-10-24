"""Shared lifecycle hook primitives for agent runs."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from loguru import logger

from mira_engine.providers.base import LLMResponse, ToolCallRequest


@dataclass(slots=True)
class AgentHookContext:
    """Mutable per-iteration state exposed to runner hooks."""

    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None

    def snapshot(self) -> AgentHookContext:
        """A detached copy: changes to it never reach the running loop."""
        return replace(
            self,
            messages=copy.deepcopy(self.messages),
            usage=dict(self.usage),
            tool_calls=list(self.tool_calls),
            tool_results=list(self.tool_results),
            tool_events=[dict(e) for e in self.tool_events],
        )


HookMode = Literal["observe", "mutate"]


class AgentHook:
    """Minimal lifecycle surface for shared runner customization.

    ``mode`` (mira#207): an ``"observe"`` hook receives a detached snapshot of
    the context and never takes part in :meth:`finalize_content`, so it cannot
    change what the model sees or returns. ``"mutate"`` hooks (the default, for
    first-party code) get the live context. ``timeout_s`` bounds every async
    call of the hook when run through :class:`CompositeHook`.
    """

    mode: HookMode = "mutate"
    timeout_s: float | None = None

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise

    def wants_streaming(self) -> bool:
        return False

    async def before_iteration(self, context: AgentHookContext) -> None:
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return content


class CompositeHook(AgentHook):
    """Fan-out hook that delegates to an ordered list of hooks.

    Error isolation: async methods catch and log per-hook exceptions
    so a faulty custom hook cannot crash the agent loop; a hook's
    ``timeout_s`` bounds each call, and ``"observe"`` hooks get a snapshot.
    ``finalize_content`` is a pipeline of ``"mutate"`` hooks (no isolation —
    bugs should surface).
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    async def _for_each_hook_safe(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        for h in self._hooks:
            call_args = args
            if getattr(h, "mode", "mutate") == "observe" and args and isinstance(args[0], AgentHookContext):
                call_args = (args[0].snapshot(), *args[1:])
            coro = getattr(h, method_name)(*call_args, **kwargs)
            timeout = getattr(h, "timeout_s", None)
            if timeout:
                coro = asyncio.wait_for(coro, timeout)
            if getattr(h, "_reraise", False):
                await coro
                continue

            try:
                await coro
            except asyncio.TimeoutError:
                logger.warning("AgentHook.{} timed out after {}s in {}; skipped", method_name, timeout, type(h).__name__)
            except Exception:
                logger.exception("AgentHook.{} error in {}", method_name, type(h).__name__)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._for_each_hook_safe("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._for_each_hook_safe("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_execute_tools", context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        for h in self._hooks:
            if getattr(h, "mode", "mutate") == "observe":
                continue  # observers can't rewrite the answer
            content = h.finalize_content(context, content)
        return content
