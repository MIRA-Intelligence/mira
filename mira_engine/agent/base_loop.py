"""Base agent loop: general-purpose message processing engine.

This module hosts :class:`BaseAgentLoop`, a tool-using LLM driver intended
for general agent workloads. It deliberately omits Mira's research-specific
behaviour (auto-mode orchestration, task-plan guardrails, automation token
budgets, agent-profile contracts). For the research-flavoured superset see
:mod:`mira_engine.agent.research_loop`.

The split keeps the upstream Nanobot mental model intact for ``mira agent``
while letting the web/UI ``ResearchAgentLoop`` extend it without diverging
from a clean baseline.
"""

from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
import re
import time
import weakref
from contextlib import AsyncExitStack, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.hook import AgentHook, AgentHookContext, CompositeHook
from mira_engine.agent.memory import Consolidator, Dream, MemoryStore
from mira_engine.agent.python_runtime_hint import build_python_runtime_hint
from mira_engine.agent.routing import ModelRouter, RoutedProviderManager
from mira_engine.agent.runner import AgentRunner
from mira_engine.agent.subagent import SubagentManager
from mira_engine.agent.tools.bg import BackgroundJobRegistry, BgTool
from mira_engine.agent.tools.cron import CronTool
from mira_engine.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from mira_engine.agent.tools.message import MessageTool
from mira_engine.agent.tools.plan import SetPlanTool
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.agent.tools.search import GlobTool, GrepTool
from mira_engine.agent.tools.shell import ExecTool
from mira_engine.agent.tools.spawn import SpawnTool
from mira_engine.agent.tools.web import WebFetchTool, WebSearchTool
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.command.router import CommandContext, CommandRouter
from mira_engine.projects import ProjectRef
from mira_engine.providers.base import LLMProvider
from mira_engine.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
    from mira_engine.cron.service import CronService

UNIFIED_SESSION_KEY = "unified:default"


class BaseAgentLoop:
    """General-purpose agent loop.

    Responsibilities:

    1. Receives messages from the bus
    2. Builds context with history, memory, and (suggested) skills
    3. Calls the LLM via :meth:`_run_agent_loop`
    4. Executes tool calls
    5. Sends responses back

    Anything specific to Mira's research workflow (auto-mode while-loop,
    task-plan guardrails, automation policies, token-budget broadcasting,
    agent profiles) lives in :class:`ResearchAgentLoop`. This class is the
    nanobot-style baseline that ``mira agent`` should target.
    """

    _TOOL_RESULT_MAX_CHARS = 500
    _RUNTIME_CHECKPOINT_KEY = "_runtime_checkpoint"
    # Marker reserved for synthetic auto-continue prompts that subclasses may
    # inject. The base loop never produces them, but ``_save_turn`` filters
    # them out so subclasses can rely on a single sentinel definition.
    _AUTO_CONTINUE_MARKER = "[AUTO-CONTINUE-INTERNAL]"

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        timezone: str | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        provider_factory: Callable[[str], LLMProvider] | None = None,
        model_router: ModelRouter | None = None,
        context_window_tokens: int | None = None,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
    ):
        from mira_engine.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider_factory = provider_factory
        self.model_router = model_router
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.context_window_tokens = context_window_tokens or 65_536
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.timezone = timezone
        self.restrict_to_workspace = restrict_to_workspace
        self._unified_session = unified_session
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self._project_sessions: dict[str, SessionManager] = {}
        self._project_contexts: dict[str, ContextBuilder] = {}
        self._project_consolidators: dict[str, Consolidator] = {}
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=reasoning_effort,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            provider_factory=provider_factory,
            model_router=model_router,
        )
        self._session_model_runtimes: dict[str, RoutedProviderManager] = {}
        self._hook = CompositeHook(list(hooks)) if hooks else None

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._last_loop_tokens_used: int = 0
        self._processing_lock = asyncio.Lock()
        # Shared registry for `exec(background=true)` jobs. Lives for the whole
        # loop lifetime so the agent can monitor jobs across many iterations,
        # and is drained on shutdown so we don't leak processes.
        self._bg_registry = BackgroundJobRegistry()
        self._register_default_tools()
        self._command_router = CommandRouter()
        from mira_engine.command.builtin import register_builtin_commands

        register_builtin_commands(self._command_router)
        generation_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", None)
        completion_tokens = (
            int(generation_max_tokens)
            if isinstance(generation_max_tokens, int | float)
            else self.max_tokens
        )
        self.consolidator = Consolidator(
            store=MemoryStore(self.workspace),
            provider=self.provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=completion_tokens,
        )
        self.dream = Dream(
            store=self.consolidator.store,
            provider=self.provider,
            model=self.model,
        )

    def _rebuild_memory_helpers(self) -> None:
        generation_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", None)
        completion_tokens = (
            int(generation_max_tokens)
            if isinstance(generation_max_tokens, int | float)
            else self.max_tokens
        )
        self.consolidator = Consolidator(
            store=MemoryStore(self.workspace),
            provider=self.provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=completion_tokens,
        )
        self.dream = Dream(
            store=self.consolidator.store,
            provider=self.provider,
            model=self.model,
        )

    async def reconfigure_runtime(
        self,
        *,
        provider: LLMProvider,
        model: str,
        provider_factory: Callable[[str], LLMProvider] | None,
        model_router: ModelRouter | None,
        workspace: Path,
        max_iterations: int,
        max_tokens: int,
        reasoning_effort: str | None,
        restrict_to_workspace: bool,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        timezone: str | None = None,
        channels_config: ChannelsConfig | None = None,
        context_window_tokens: int | None = None,
    ) -> None:
        """Apply UI-saved runtime config to the live agent loop."""
        async with self._processing_lock:
            next_workspace = workspace.expanduser()
            workspace_changed = next_workspace != self.workspace

            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                finally:
                    self._mcp_stack = None
                    self._mcp_connected = False
                    self._mcp_connecting = False

            self.provider = provider
            self.model = model or provider.get_default_model()
            self.provider_factory = provider_factory
            self.model_router = model_router
            self.workspace = next_workspace
            self.max_iterations = max_iterations
            self.max_tokens = max_tokens
            self.reasoning_effort = reasoning_effort
            self.brave_api_key = brave_api_key
            self.web_proxy = web_proxy
            if exec_config is not None:
                self.exec_config = exec_config
            self.timezone = timezone
            self.restrict_to_workspace = restrict_to_workspace
            if channels_config is not None:
                self.channels_config = channels_config
            if context_window_tokens is not None:
                self.context_window_tokens = context_window_tokens

            if workspace_changed:
                self.context = ContextBuilder(next_workspace)
                self.sessions = SessionManager(next_workspace)
                self._project_sessions.clear()

            self._session_model_runtimes.clear()

            self.subagents.provider = provider
            self.subagents.model = self.model
            self.subagents.workspace = next_workspace
            self.subagents.temperature = self.temperature
            self.subagents.max_tokens = self.max_tokens
            self.subagents.reasoning_effort = self.reasoning_effort
            self.subagents.brave_api_key = self.brave_api_key
            self.subagents.web_proxy = self.web_proxy
            self.subagents.exec_config = self.exec_config
            self.subagents.restrict_to_workspace = self.restrict_to_workspace
            self.subagents.provider_factory = provider_factory
            self.subagents.model_router = model_router
            self.subagents.runner = AgentRunner(provider)
            self.subagents._session_runtimes.clear()

            self.tools = ToolRegistry()
            self._register_default_tools()
            self._rebuild_memory_helpers()

            logger.info("Agent runtime reconfigured: model={}, workspace={}", self.model, self.workspace)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        skill_access_dirs: list[Path] = []
        if self.restrict_to_workspace:
            from mira_engine.agent.skills import SkillsLoader

            def _add_skill_dir(path: Path) -> None:
                try:
                    resolved = path.resolve()
                except Exception:
                    return
                if resolved != self.workspace and resolved not in skill_access_dirs:
                    skill_access_dirs.append(resolved)

            skills_loader = SkillsLoader(self.workspace)
            for root in skills_loader.workspace_skills_roots:
                _add_skill_dir(root)
            if skills_loader.builtin_skills:
                _add_skill_dir(skills_loader.builtin_skills)
            for skill in skills_loader.list_skills(filter_unavailable=False):
                skill_path = Path(skill["path"])
                _add_skill_dir(skill_path.parent)
                parent = skill_path.parent.parent
                if parent != skill_path.parent:
                    _add_skill_dir(parent)

        supports_vision = self.provider.supports_vision(self.model)
        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=skill_access_dirs, supports_vision=supports_vision))
        self.tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=skill_access_dirs))
        self.tools.register(
            GrepTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                extra_allowed_dirs=skill_access_dirs,
            )
        )
        self.tools.register(
            GlobTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                extra_allowed_dirs=skill_access_dirs,
            )
        )
        if self.exec_config.enable:
            self.tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
                background_registry=self._bg_registry,
                enable_background=True,
                python_runtime=self.exec_config.python,
            ))
            self.tools.register(BgTool(registry=self._bg_registry))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SetPlanTool())
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            cron_tool = CronTool(self.cron_service)
            setattr(cron_tool, "_default_timezone", self.timezone)
            self.tools.register(cron_tool)

    def _skill_access_dirs_for_workspace(self, workspace: Path) -> list[Path]:
        skill_access_dirs: list[Path] = []
        if not self.restrict_to_workspace:
            return skill_access_dirs

        from mira_engine.agent.skills import SkillsLoader

        def _add_skill_dir(path: Path) -> None:
            try:
                resolved = path.resolve()
            except Exception:
                return
            if resolved != workspace and resolved not in skill_access_dirs:
                skill_access_dirs.append(resolved)

        skills_loader = SkillsLoader(workspace)
        for root in skills_loader.workspace_skills_roots:
            _add_skill_dir(root)
        if skills_loader.builtin_skills:
            _add_skill_dir(skills_loader.builtin_skills)
        for skill in skills_loader.list_skills(filter_unavailable=False):
            skill_path = Path(skill["path"])
            _add_skill_dir(skill_path.parent)
            parent = skill_path.parent.parent
            if parent != skill_path.parent:
                _add_skill_dir(parent)
        return skill_access_dirs

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from mira_engine.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    @staticmethod
    def _project_ref_from_metadata(metadata: dict[str, Any] | None) -> ProjectRef | None:
        meta = metadata or {}
        raw_project_dir = meta.get("project_dir")
        if not isinstance(raw_project_dir, str) or not raw_project_dir.strip():
            return None
        try:
            project_dir = Path(raw_project_dir).expanduser().resolve(strict=False)
        except OSError:
            return None
        raw_project_id = meta.get("project_id")
        project_id = raw_project_id.strip() if isinstance(raw_project_id, str) and raw_project_id.strip() else project_dir.name
        return ProjectRef(project_id=project_id, project_dir=project_dir, metadata=meta)

    @staticmethod
    def _project_scope_key(project_ref: ProjectRef) -> str:
        project_dir = project_ref.project_dir.expanduser().resolve(strict=False)
        return hashlib.sha256(str(project_dir).encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _scoped_session_key(cls, project_ref: ProjectRef | None, session_key: str) -> str:
        if project_ref is None:
            return session_key
        return f"project:{cls._project_scope_key(project_ref)}:{session_key}"

    def _get_project_sessions(self, project_ref: ProjectRef | str) -> SessionManager:
        """Return a per-project SessionManager, creating one if needed."""

        if isinstance(project_ref, ProjectRef):
            cache_key = self._project_scope_key(project_ref)
            project_dir = project_ref.project_dir
        else:
            project_dir = Path(project_ref)
            cache_key = str(project_dir.expanduser().resolve(strict=False))
        manager = self._project_sessions.get(cache_key)
        if manager is None or manager.workspace.resolve(strict=False) != project_dir.resolve(strict=False):
            manager = SessionManager(project_dir)
            self._project_sessions[cache_key] = manager
        return manager

    def _get_project_context(self, project_ref: ProjectRef | None) -> ContextBuilder:
        if project_ref is None:
            return self.context
        cache_key = self._project_scope_key(project_ref)
        ctx = self._project_contexts.get(cache_key)
        if ctx is None or ctx.workspace.resolve(strict=False) != project_ref.project_dir.resolve(strict=False):
            ctx = ContextBuilder(project_ref.project_dir)
            self._project_contexts[cache_key] = ctx
        return ctx

    def _get_project_consolidator(
        self,
        project_ref: ProjectRef | None,
        sessions_mgr: SessionManager,
        ctx: ContextBuilder,
    ) -> Consolidator:
        if project_ref is None:
            return self.consolidator
        cache_key = self._project_scope_key(project_ref)
        consolidator = self._project_consolidators.get(cache_key)
        if consolidator is None or getattr(consolidator.store, "workspace", None) != project_ref.project_dir:
            generation_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", None)
            completion_tokens = (
                int(generation_max_tokens)
                if isinstance(generation_max_tokens, int | float)
                else self.max_tokens
            )
            consolidator = Consolidator(
                store=MemoryStore(project_ref.project_dir),
                provider=self.provider,
                model=self.model,
                sessions=sessions_mgr,
                context_window_tokens=self.context_window_tokens,
                build_messages=ctx.build_messages,
                get_tool_definitions=self.tools.get_definitions,
                max_completion_tokens=completion_tokens,
            )
            self._project_consolidators[cache_key] = consolidator
        return consolidator

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        project_ref: ProjectRef | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        if project_ref is not None:
            allowed_dir = project_ref.project_dir if self.restrict_to_workspace else None
            extra_allowed_dirs = self._skill_access_dirs_for_workspace(project_ref.project_dir)
            for tool in getattr(self.tools, "_tools", {}).values():
                if hasattr(tool, "set_runtime_context"):
                    tool.set_runtime_context(
                        workspace=project_ref.project_dir,
                        allowed_dir=allowed_dir,
                        extra_allowed_dirs=extra_allowed_dirs,
                    )
                if hasattr(tool, "set_project_context"):
                    tool.set_project_context(project_ref.project_id, str(project_ref.project_dir))
        else:
            for tool in getattr(self.tools, "_tools", {}).values():
                if hasattr(tool, "clear_runtime_context"):
                    tool.clear_runtime_context()
                if hasattr(tool, "clear_project_context"):
                    tool.clear_project_context()

        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))
                if session_key and hasattr(tool, "set_session_key"):
                    tool.set_session_key(session_key)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text)
        cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned)
        return cleaned.strip() or None

    @staticmethod
    async def _emit_activity_ping(on_progress: Callable[..., Awaitable[None]]) -> None:
        """Tell UI clients the engine is active without exposing tool details."""
        try:
            params = inspect.signature(on_progress).parameters
        except (TypeError, ValueError):
            params = {}
        supports_activity_ping = "activity_ping" in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        )
        if supports_activity_ping:
            await on_progress("Mira is working...", activity_ping=True)

    @classmethod
    async def _activity_ping_loop(
        cls,
        on_progress: Callable[..., Awaitable[None]],
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        """Keep long-running tool calls visibly alive in UI clients."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await cls._emit_activity_ping(on_progress)
            except Exception as exc:
                logger.debug("Activity ping failed; stopping heartbeat: {}", exc)
                return

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            if not isinstance(args, dict):
                return tc.name
            if tc.name == "read_file":
                path = args.get("path")
                if isinstance(path, str) and path:
                    return f"read {path}"
            val = next(iter(args.values()), None)
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _extract_read_file_path(arguments: object) -> str | None:
        """Extract read_file path argument from model tool-call payload."""
        payload = arguments[0] if isinstance(arguments, list) and arguments else arguments
        if not isinstance(payload, dict):
            return None
        value = payload.get("path")
        return value if isinstance(value, str) else None

    @staticmethod
    def _extract_skill_name_from_path(path: str) -> str | None:
        """Return skill name when path targets a skills/**/SKILL.md file."""
        normalized = path.strip().replace("\\", "/")
        if not normalized.lower().endswith("/skill.md"):
            return None

        parts = [part for part in normalized.split("/") if part]
        if len(parts) < 2 or parts[-1].lower() != "skill.md":
            return None
        return parts[-2]

    @classmethod
    def _build_skill_invoked_event(
        cls,
        *,
        tool_name: str,
        arguments: object,
    ) -> dict[str, Any] | None:
        """Build audit payload when agent reads a skill file."""
        if tool_name != "read_file":
            return None
        path = cls._extract_read_file_path(arguments)
        if not path:
            return None
        skill_name = cls._extract_skill_name_from_path(path)
        if not skill_name:
            return None
        return {
            "tool": tool_name,
            "skill_name": skill_name,
            "path": path,
        }

    @staticmethod
    def _route_hint(
        tier: str,
        model: str,
        candidates: tuple[str, ...],
        score: int | None,
        source: str,
        reason: str | None,
    ) -> str:
        """Format a visible routing hint for progress output."""
        details = f", {source}"
        if candidates and model != candidates[0]:
            details += f", fallback_from={candidates[0]}"
        if reason:
            details += f", reason={reason[:80]}"
        if score is None:
            return f"router -> {tier} ({model}{details})"
        return f"router -> {tier} ({model}, score={score}{details})"

    def _compose_extra_system(
        self,
        ui_system_instructions: object,
        guard_notice: object,
    ) -> str | None:
        """Merge optional UI instructions, guardrail notices, and a venv
        usage hint when ``tools.exec.python.manager`` is active."""
        base = (
            ui_system_instructions.strip()
            if isinstance(ui_system_instructions, str) and ui_system_instructions.strip()
            else ""
        )
        notice = (
            guard_notice.strip()
            if isinstance(guard_notice, str) and guard_notice.strip()
            else ""
        )
        python_hint = build_python_runtime_hint(
            getattr(getattr(self, "exec_config", None), "python", None)
        ) or ""
        sections = [chunk for chunk in (python_hint, base, notice) if chunk]
        return "\n\n".join(sections) if sections else None

    def _get_model_runtime(self, session_key: str) -> RoutedProviderManager:
        """Return the session-local model runtime, creating it on demand."""
        runtime = self._session_model_runtimes.get(session_key)
        if runtime is None:
            runtime = RoutedProviderManager(
                default_provider=self.provider,
                default_model=self.model,
                router=self.model_router,
                provider_factory=self.provider_factory,
            )
            self._session_model_runtimes[session_key] = runtime
        return runtime

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        model_runtime: RoutedProviderManager | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        active_provider: LLMProvider | None = None
        active_route = None
        loop_tokens_used = 0
        hook = getattr(self, "_hook", None)

        while iteration < self.max_iterations:
            hook_ctx = AgentHookContext(iteration=iteration, messages=messages)
            if hook:
                await hook.before_iteration(hook_ctx)
            iteration += 1

            use_routed_runtime = model_runtime is not None and (
                self.model_router is not None or self.provider_factory is not None
            )
            if not use_routed_runtime:
                if on_stream is not None and hasattr(self.provider, "chat_stream_with_retry"):
                    streamed_raw = ""
                    streamed_clean = ""

                    async def _stream_delta(delta: str) -> None:
                        nonlocal streamed_raw, streamed_clean
                        if not delta:
                            return
                        streamed_raw += delta
                        new_clean = self._strip_think(streamed_raw) or ""
                        if not new_clean:
                            streamed_clean = ""
                            return
                        if new_clean.startswith(streamed_clean):
                            out = new_clean[len(streamed_clean):]
                        else:
                            out = new_clean
                        streamed_clean = new_clean
                        if out and on_stream:
                            await on_stream(out)

                    response = await self.provider.chat_stream_with_retry(
                        model=self.model,
                        messages=messages,
                        tools=self.tools.get_definitions(),
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                        on_content_delta=_stream_delta,
                    )
                    clean_streamed = self._strip_think(streamed_raw)
                    if response.content and clean_streamed:
                        response.content = clean_streamed
                    if on_stream_end:
                        await on_stream_end(resuming=False)
                else:
                    response = await self.provider.chat_with_retry(
                        model=self.model,
                        messages=messages,
                        tools=self.tools.get_definitions(),
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                    )
            else:
                if active_provider is None or active_route is None:
                    active_provider, active_route = await model_runtime.resolve(messages, iteration)
                response, active_route = await model_runtime.chat(
                    active_route,
                    messages=messages,
                    tools=self.tools.get_definitions(),
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    reasoning_effort=self.reasoning_effort,
                )
            if isinstance(response.usage, dict):
                self._last_usage = {
                    "prompt_tokens": int(response.usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(response.usage.get("completion_tokens", 0) or 0),
                    "cached_tokens": int(response.usage.get("cached_tokens", 0) or 0),
                }
                usage_total = response.usage.get("total_tokens")
                if isinstance(usage_total, int) and usage_total > 0:
                    loop_tokens_used += usage_total
            hook_ctx.response = response
            hook_ctx.usage = dict(response.usage or {})
            hook_ctx.tool_calls = list(response.tool_calls or [])

            if (
                iteration == 1
                and on_progress
                and self.model_router
                and self.model_router.enabled
                and active_route is not None
            ):
                await on_progress(
                    self._route_hint(
                        active_route.tier,
                        active_route.model,
                        active_route.candidates,
                        active_route.score,
                        active_route.source,
                        active_route.reason,
                    )
                )

            if response.has_tool_calls:
                if on_progress:
                    await self._emit_activity_ping(on_progress)
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                if hook:
                    await hook.before_execute_tools(hook_ctx)

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    if audit_hook:
                        skill_event = self._build_skill_invoked_event(
                            tool_name=tool_call.name,
                            arguments=tool_call.arguments,
                        )
                        if skill_event:
                            await audit_hook(skill_event)
                    ping_task: asyncio.Task[None] | None = None
                    if on_progress:
                        ping_task = asyncio.create_task(self._activity_ping_loop(on_progress))
                    try:
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    finally:
                        if ping_task is not None:
                            ping_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await ping_task
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                    hook_ctx.tool_results.append(result)
                    hook_ctx.tool_events.append(
                        {"name": tool_call.name, "status": "ok", "detail": str(result)}
                    )
            else:
                clean = self._strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    # Save a neutral placeholder so the session doesn't end
                    # with an orphaned user message (consecutive users cause
                    # permanent 400 loops with strict providers like Anthropic).
                    messages = self.context.add_assistant_message(
                        messages, "(error — see previous log)"
                    )
                    hook_ctx.final_content = final_content
                    hook_ctx.stop_reason = "error"
                    if hook:
                        await hook.after_iteration(hook_ctx)
                    break
                if clean is None and iteration < self.max_iterations:
                    logger.warning("Received think-only/empty final response, retrying")
                    continue
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                if hook:
                    clean = hook.finalize_content(hook_ctx, clean)
                final_content = clean
                hook_ctx.final_content = final_content
                hook_ctx.stop_reason = "completed"
                if hook:
                    await hook.after_iteration(hook_ctx)
                break

            if hook:
                await hook.after_iteration(hook_ctx)

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        self._last_loop_tokens_used = loop_tokens_used
        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            control = (msg.metadata or {}).get("_control")
            if control and await self._handle_control(msg, control):
                continue
            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            elif self._command_router.is_priority(msg.content):
                project_ref = self._project_ref_from_metadata(msg.metadata)
                key = (
                    UNIFIED_SESSION_KEY
                    if self._unified_session and not msg.session_key_override
                    else msg.session_key
                )
                scoped_key = self._scoped_session_key(project_ref, key)
                sessions_mgr = self._get_project_sessions(project_ref) if project_ref else self.sessions
                session = sessions_mgr.get_or_create(key)
                if project_ref is not None:
                    msg.metadata = {
                        **dict(msg.metadata or {}),
                        "project_id": project_ref.project_id,
                        "project_dir": str(project_ref.project_dir),
                    }
                ctx = CommandContext(
                    msg=msg,
                    session=session,
                    key=scoped_key,
                    raw=msg.content.strip(),
                    loop=self,
                )
                response = await self._command_router.dispatch_priority(ctx)
                if response is not None:
                    await self.bus.publish_outbound(response)
            else:
                project_ref = self._project_ref_from_metadata(msg.metadata)
                effective_key = (
                    UNIFIED_SESSION_KEY if self._unified_session and not msg.session_key_override else msg.session_key
                )
                effective_key = self._scoped_session_key(project_ref, effective_key)
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(effective_key, []).append(task)
                task.add_done_callback(lambda t, k=effective_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_control(self, msg: InboundMessage, control: str) -> bool:
        """Hook for subclasses to handle ``_control`` metadata messages.

        The base loop has no control messages of its own and always returns
        ``False``. Subclasses (notably :class:`ResearchAgentLoop`) override
        this to handle entries such as ``set_mode`` without forking ``run``.
        """
        return False

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        project_ref = self._project_ref_from_metadata(msg.metadata)
        raw_key = (
            UNIFIED_SESSION_KEY
            if getattr(self, "_unified_session", False) and not msg.session_key_override
            else msg.session_key
        )
        scoped_key = self._scoped_session_key(project_ref, raw_key)
        tasks = self._active_tasks.pop(scoped_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(scoped_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
            metadata=dict(msg.metadata or {}),
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        if getattr(self, "_unified_session", False) and not msg.session_key_override:
            msg.session_key_override = UNIFIED_SESSION_KEY

        if bool((msg.metadata or {}).get("_wants_stream")):
            stream_meta = dict(msg.metadata or {})
            streamed_any = False

            async def _on_stream(delta: str) -> None:
                nonlocal streamed_any
                if not delta:
                    return
                streamed_any = True
                meta = dict(stream_meta)
                meta["_stream_delta"] = True
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=delta,
                        metadata=meta,
                    )
                )

            async def _on_stream_end(*, resuming: bool = False) -> None:
                if resuming:
                    return
                meta = dict(stream_meta)
                meta["_stream_end"] = True
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata=meta,
                    )
                )

            try:
                response = await self._process_message(
                    msg, on_stream=_on_stream, on_stream_end=_on_stream_end
                )
                # Fallback: if the runtime never streamed (e.g. routed provider
                # without token streaming), the deltas/end markers carry no
                # content, so publish the final response normally so the client
                # still receives the answer.
                if not streamed_any and response is not None:
                    await self.bus.publish_outbound(response)
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                err_text = "Sorry, I encountered an error."
                if msg.channel == "cli":
                    err_text += " Run `mira agent --logs` to view details."
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=err_text,
                    metadata=dict(msg.metadata or {}),
                ))
            return
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                err_text = "Sorry, I encountered an error."
                if msg.channel == "cli":
                    err_text += " Run `mira agent --logs` to view details."
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=err_text,
                    metadata=dict(msg.metadata or {}),
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._consolidation_tasks:
            await asyncio.gather(*list(self._consolidation_tasks), return_exceptions=True)
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None
        # Drain background jobs alongside MCP because every shutdown path that
        # cares about clean teardown already calls ``close_mcp``. Best-effort:
        # we never let a stuck child block engine exit.
        try:
            await self._bg_registry.shutdown()
        except Exception:
            logger.exception("Failed to shut down background job registry")

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    def _on_session_reset(self, session_key: str) -> None:
        """Hook fired after a ``/new`` reset clears the session messages.

        Base implementation only drops the cached model runtime so the next
        message rebuilds routing state. Subclasses may extend this to clear
        their own per-session caches (e.g. token totals).
        """
        self._session_model_runtimes.pop(session_key, None)

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response.

        This is the nanobot-style baseline: build context, invoke the agent
        loop once, persist the turn, return the answer. No auto-mode loop,
        no task-plan guardrails, no automation token budget. Mira's research
        flavoured override lives in :class:`ResearchAgentLoop`.
        """
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            meta = msg.metadata or {}
            project_ref = self._project_ref_from_metadata(meta)
            raw_key = f"{channel}:{chat_id}"
            key = self._scoped_session_key(project_ref, raw_key)
            sessions_mgr = self._get_project_sessions(project_ref) if project_ref else self.sessions
            session = sessions_mgr.get_or_create(raw_key)
            model_runtime = self._get_model_runtime(key)
            self._set_tool_context(
                channel,
                chat_id,
                meta.get("message_id"),
                project_ref=project_ref,
                session_key=key,
            )
            history = session.get_history(max_messages=self.memory_window)
            ctx = self._get_project_context(project_ref)
            messages = ctx.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                project_dir=str(project_ref.project_dir) if project_ref else None,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages, model_runtime=model_runtime)
            self._save_turn(session, all_msgs, 1 + len(history))
            sessions_mgr.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = msg.metadata or {}
        project_ref = self._project_ref_from_metadata(meta)
        project_dir = str(project_ref.project_dir) if project_ref else None
        raw_key = session_key or msg.session_key
        key = self._scoped_session_key(project_ref, raw_key)
        sessions_mgr = self._get_project_sessions(project_ref) if project_ref else self.sessions

        session = sessions_mgr.get_or_create(raw_key)
        memory_workspace = project_ref.project_dir if project_ref else self.workspace
        ctx = self._get_project_context(project_ref)
        project_consolidator = self._get_project_consolidator(project_ref, sessions_mgr, ctx)
        recent_skill_names: list[str] = []
        if isinstance(session.metadata, dict):
            raw_recent = session.metadata.get("_recent_skills")
            if isinstance(raw_recent, list):
                recent_skill_names = [str(s) for s in raw_recent if isinstance(s, str)]

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            if msg.channel == "cli":
                snapshot = session.messages[session.last_consolidated:]
                session.clear()
                sessions_mgr.save(session)
                sessions_mgr.invalidate(session.key)
                self._on_session_reset(key)
                if snapshot:
                    self._schedule_background(project_consolidator.archive(snapshot))
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="New session started.")
            ok = await self._consolidate_memory(session, archive_all=True, workspace_override=memory_workspace)
            if not ok:
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="Memory archival failed. Session was not reset.")
            session.clear()
            sessions_mgr.save(session)
            sessions_mgr.invalidate(session.key)
            self._on_session_reset(key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            ctx = CommandContext(
                msg=msg,
                session=session,
                key=key,
                raw=msg.content.strip(),
                loop=self,
            )
            handled = await self._command_router.dispatch(ctx)
            if handled is not None:
                return handled

        if cmd.startswith("/"):
            ctx = CommandContext(
                msg=msg,
                session=session,
                key=key,
                raw=msg.content.strip(),
                loop=self,
            )
            handled = await self._command_router.dispatch(ctx)
            if handled is not None:
                return handled

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and key not in self._consolidating):
            self._consolidating.add(key)
            lock = self._consolidation_locks.setdefault(key, asyncio.Lock())
            _mw = memory_workspace

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session, workspace_override=_mw)
                finally:
                    self._consolidating.discard(key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(
            msg.channel,
            msg.chat_id,
            meta.get("message_id"),
            project_ref=project_ref,
            session_key=key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        await project_consolidator.maybe_consolidate_by_tokens(session)
        history = session.get_history(max_messages=self.memory_window)
        model_runtime = self._get_model_runtime(key)
        extra_system = self._compose_extra_system(
            meta.get("_ui_system_instructions"),
            meta.get("_task_plan_guard_notice"),
        )

        suggested_skills = ctx.skills.suggest_skills(
            msg.content,
            recent=recent_skill_names,
            limit=3,
        )
        active_skills: list[str] = []
        for name in [*recent_skill_names, *suggested_skills]:
            if name not in active_skills:
                active_skills.append(name)
        active_skills = active_skills[-4:]
        skill_hint = ""
        if suggested_skills:
            skill_hint = (
                "Skill routing hint: this request likely matches one or more skills. "
                "Before answering, use read_file to inspect these SKILL.md files if relevant:\n"
                + "\n".join(f"- {name}" for name in suggested_skills)
            )
            if on_progress:
                try:
                    await on_progress(
                        f"skill router -> {', '.join(suggested_skills)}",
                        tool_hint=True,
                    )
                except TypeError:
                    await on_progress(f"skill router -> {', '.join(suggested_skills)}")
        if extra_system:
            extra_system = skill_hint + "\n\n" + extra_system if skill_hint else extra_system
        else:
            extra_system = skill_hint or None
        initial_messages = ctx.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=active_skills or None,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            project_dir=project_dir,
            extra_system=extra_system,
        )

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            activity_ping: bool = False,
        ) -> None:
            progress_meta = dict(msg.metadata or {})
            progress_meta["_progress"] = True
            progress_meta["_tool_hint"] = tool_hint
            if activity_ping:
                progress_meta["_activity_ping"] = True
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=progress_meta,
            ))

        progress_cb = on_progress or _bus_progress
        current_turn_skills: set[str] = set()
        audit_cb = None
        emit_audit_to_channel = msg.channel == "ui" or bool(meta.get("_emit_skill_audit"))
        if emit_audit_to_channel or audit_hook:
            async def _audit(details: dict[str, Any]) -> None:
                skill_name = details.get("skill_name")
                if isinstance(skill_name, str) and skill_name.strip():
                    current_turn_skills.add(skill_name.strip())
                if audit_hook:
                    await audit_hook(details)
                if not emit_audit_to_channel:
                    return
                metadata = dict(msg.metadata or {})
                metadata["_audit_only"] = True
                metadata["_audit_event"] = "skill_invoked"
                metadata["_audit_details"] = details
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata=metadata,
                ))

            audit_cb = _audit
        run_kwargs: dict[str, Any] = {
            "model_runtime": model_runtime,
            "on_progress": progress_cb,
            "audit_hook": audit_cb,
        }
        if on_stream is not None:
            run_kwargs["on_stream"] = on_stream
        if on_stream_end is not None:
            run_kwargs["on_stream_end"] = on_stream_end
        await self._emit_activity_ping(progress_cb)
        final_content, _, all_msgs = await self._run_agent_loop(initial_messages, **run_kwargs)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        if isinstance(session.metadata, dict):
            prior = session.metadata.get("_recent_skills")
            merged: list[str] = []
            if isinstance(prior, list):
                for item in prior:
                    if isinstance(item, str) and item not in merged:
                        merged.append(item)
            for item in sorted(current_turn_skills):
                if item not in merged:
                    merged.append(item)
            session.metadata["_recent_skills"] = merged[-10:]

        self._save_turn(session, all_msgs, 1 + len(history))
        sessions_mgr.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=dict(msg.metadata or {}),
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                if len(content) > getattr(self, "max_tool_result_chars", self._TOOL_RESULT_MAX_CHARS):
                    cap = int(getattr(self, "max_tool_result_chars", self._TOOL_RESULT_MAX_CHARS))
                    entry["content"] = content[:cap] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if (
                    isinstance(entry.get("content"), str)
                    and self._AUTO_CONTINUE_MARKER in entry["content"]
                ):
                    continue

            # Replace base64 image blocks with text placeholders for ANY role
            # (tool/assistant/user) so they never persist in the session log:
            # text-only providers reject `image_url` content blocks (e.g.
            # Moonshot's "unknown variant image_url, expected text"), and the
            # UI cannot render base64. The live turn keeps the real image; it is
            # re-read on demand if the model needs it again.
            cur = entry.get("content")
            if isinstance(cur, list):
                if role == "user":
                    # Drop runtime-context text blocks from multimodal messages.
                    cur = [
                        c
                        for c in cur
                        if not (
                            isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                        )
                    ]
                filtered = self._strip_image_blocks(cur)
                if not filtered:
                    continue
                entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    @staticmethod
    def _strip_image_blocks(content: list[Any]) -> list[Any]:
        """Replace base64 ``image_url`` blocks with short text placeholders.

        Non-image blocks pass through untouched. Used by ``_save_turn`` so a
        multimodal message (e.g. a ``read_file`` image result) is never written
        to the session log as raw base64.
        """
        filtered: list[Any] = []
        for c in content:
            if not isinstance(c, dict):
                filtered.append(c)
                continue
            image_url = c.get("image_url")
            url = image_url.get("url", "") if isinstance(image_url, dict) else ""
            if c.get("type") == "image_url" and isinstance(url, str) and url.startswith("data:image/"):
                ctx_meta = c.get("_meta")
                path = ctx_meta.get("path") if isinstance(ctx_meta, dict) else None
                filtered.append({"type": "text", "text": f"[image: {path}]" if path else "[image]"})
            else:
                filtered.append(c)
        return filtered

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        checkpoint = (session.metadata or {}).get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant = checkpoint.get("assistant_message")
        completed = checkpoint.get("completed_tool_results") or []
        pending = checkpoint.get("pending_tool_calls") or []
        if not isinstance(assistant, dict):
            if isinstance(session.metadata, dict):
                session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)
            return False

        reconstructed: list[dict[str, Any]] = [assistant]
        for item in completed:
            if isinstance(item, dict):
                reconstructed.append(item)
        for tc in pending:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            reconstructed.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": fn.get("name") or "tool",
                    "content": "Tool execution was interrupted before this tool finished.",
                }
            )

        existing = list(session.messages or [])
        if existing == reconstructed:
            pass
        elif len(existing) < len(reconstructed) and existing == reconstructed[: len(existing)]:
            session.messages = reconstructed
        elif len(existing) >= len(reconstructed) and existing[-len(reconstructed) :] == reconstructed:
            pass
        else:
            session.messages = reconstructed

        if isinstance(session.metadata, dict):
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)
        return True

    async def _consolidate_memory(
        self, session, archive_all: bool = False, workspace_override: Path | None = None,
    ) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        ws = workspace_override or self.workspace
        return await MemoryStore(ws).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | str | None:
        """Process a message directly (for CLI or cron usage).

        ``metadata`` is forwarded into the synthesised :class:`InboundMessage`
        so callers can inject CLI flags (``run_mode``, ``agent_profile``,
        ``automation_policy``, ``project_dir``, …) that subclasses interpret.
        """
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            metadata=dict(metadata or {}),
        )
        response = await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            audit_hook=audit_hook,
        )
        if response is None:
            return ""
        if isinstance(response, OutboundMessage) and isinstance(content, str) and content.strip().startswith("/"):
            return response
        if isinstance(response, OutboundMessage) and channel == "cli":
            return response.content
        return response

    def _schedule_background(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Track background coroutines so shutdown can await completion."""
        task = asyncio.create_task(coro)
        self._consolidation_tasks.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            self._consolidation_tasks.discard(done)

        task.add_done_callback(_cleanup)
        return task


__all__ = ["BaseAgentLoop", "UNIFIED_SESSION_KEY"]
