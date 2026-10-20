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
import hashlib
import inspect
import json
import re
import time
import uuid
import weakref
from contextlib import AsyncExitStack, nullcontext, suppress
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from mira_engine.observability import correlation_id, diagnostic_context, emit_event, opaque_ref

from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.token_usage import record_token_usage, track_token_usage
from mira_engine.agent.hook import AgentHook, AgentHookContext, CompositeHook
from mira_engine.agent.memory import Consolidator, Dream, MemoryStore
from mira_engine.agent.python_runtime_hint import build_python_runtime_hint
from mira_engine.agent.routing import RoutedProviderManager
from mira_engine.agent.runner import AgentRunner
from mira_engine.agent.skill_events import skill_loaded, skill_read_details
from mira_engine.agent.subagent import SubagentManager
from mira_engine.agent.tools.bg import BackgroundJobRegistry, BgTool
from mira_engine.agent.tools.community import build_community_tools
from mira_engine.agent.tools.consult import ConsultRoleTool
from mira_engine.agent.tools.cron import CronTool
from mira_engine.agent.tools.knowledge import KnowledgeReadTool, KnowledgeScope, KnowledgeSearchTool
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
from mira_engine.community.privacy import COMMUNITY_CHANNEL
from mira_engine.modules.tools import (
    InitiationResearchResultTool,
    PubMedSearchTool,
    WorkflowArtifactTool,
    WorkflowCompletionTool,
    WorkflowExperimentTool,
    WorkflowInteractionTool,
)
from mira_engine.projects import ProjectRef
from mira_engine.providers.base import LLMProvider
from mira_engine.session.manager import Session, SessionManager
from mira_engine.utils.helpers import normalize_citations

if TYPE_CHECKING:
    from mira_engine.config.schema import ChannelsConfig, CommunityConfig, ExecToolConfig, KnowledgeConfig
    from mira_engine.cron.service import CronService

UNIFIED_SESSION_KEY = "unified:default"

# Whether the last _run_agent_loop in this task ended on text the model streamed.
# Synthesized endings (iteration limit, stop guards, errors) are never streamed,
# so the dispatcher must still publish them or the client keeps waiting.
_final_content_streamed: ContextVar[bool] = ContextVar("mira_final_content_streamed", default=False)

# Why the current turn ended, reported to the UI in its turn_finished event so
# the client never has to infer the end of a turn from message text.
TURN_COMPLETED = "completed"
TURN_MAX_ITERATIONS = "max_iterations"
TURN_MAX_TOKENS = "max_tokens"
TURN_WAITING_INPUT = "waiting_input"
TURN_BUDGET_EXCEEDED = "budget_exceeded"
TURN_STOPPED = "stopped"
TURN_ERROR = "error"
_turn_stop_reason: ContextVar[str] = ContextVar("mira_turn_stop_reason", default=TURN_COMPLETED)


# reconfigure_runtime: leave this setting as it is.
_KEEP: Any = object()


def set_turn_stop_reason(reason: str) -> None:
    """Record why the turn running in this task ended (last writer wins)."""
    _turn_stop_reason.set(reason)


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
        knowledge_config: KnowledgeConfig | None = None,
        cron_service: CronService | None = None,
        timezone: str | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        community_config: CommunityConfig | None = None,
        provider_factory: Callable[[str], LLMProvider] | None = None,
        model_candidates: list[str] | None = None,
        role_provider_factory: "Callable[[str], tuple[LLMProvider, str, tuple[str, ...]]] | None" = None,
        context_window_tokens: int | None = None,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
    ):
        from mira_engine.config.schema import ExecToolConfig, KnowledgeConfig
        self.bus = bus
        self.channels_config = channels_config
        self.community_config = community_config
        self.provider_factory = provider_factory
        self.role_provider_factory = role_provider_factory
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.model_candidates = list(model_candidates) if model_candidates else None
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.context_window_tokens = context_window_tokens or 65_536
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.knowledge_config = knowledge_config or KnowledgeConfig()
        self._knowledge_scope: KnowledgeScope | None = None
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
            model_candidates=self.model_candidates,
            role_provider_factory=role_provider_factory,
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
        # Runtime swap saved while a turn held the lock; applied when it ends.
        self._pending_runtime_swap: Callable[[], Awaitable[None]] | None = None
        self._runtime_swap_task: asyncio.Task[None] | None = None
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
        model_candidates: list[str] | None = None,
        role_provider_factory: "Callable[[str], tuple[LLMProvider, str, tuple[str, ...]]] | None" = None,
        workspace: Path,
        max_iterations: int,
        max_tokens: int,
        reasoning_effort: str | None,
        restrict_to_workspace: bool,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        knowledge_config: KnowledgeConfig | None = None,
        timezone: str | None = None,
        channels_config: ChannelsConfig | None = None,
        community_config: CommunityConfig | None = None,
        context_window_tokens: int | None = None,
        temperature: float | None | object = _KEEP,
        auto_max_rounds: int | None = None,
    ) -> None:
        """Apply UI-saved runtime config to the live agent loop.

        Limits (iterations, tokens, reasoning, temperature, auto rounds) take
        effect at once, even mid-turn: the loop reads them on every LLM call.
        Swapping the provider, workspace, tools and MCP connections waits for
        the running turn, but in the background, so saving settings never
        blocks behind a long turn.
        """
        self._apply_runtime_limits(
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            auto_max_rounds=auto_max_rounds,
        )

        async def _swap() -> None:
            await self._swap_runtime(
                provider=provider,
                model=model,
                provider_factory=provider_factory,
                model_candidates=model_candidates,
                role_provider_factory=role_provider_factory,
                workspace=workspace,
                restrict_to_workspace=restrict_to_workspace,
                brave_api_key=brave_api_key,
                web_proxy=web_proxy,
                exec_config=exec_config,
                knowledge_config=knowledge_config,
                timezone=timezone,
                channels_config=channels_config,
                community_config=community_config,
                context_window_tokens=context_window_tokens,
            )

        idle = not self._processing_lock.locked() and (
            self._runtime_swap_task is None or self._runtime_swap_task.done()
        )
        if idle:
            # Nothing running: swap now, so errors reach the caller as before.
            self._pending_runtime_swap = None
            async with self._processing_lock:
                await _swap()
            return
        # Newest settings win if several saves arrive during one turn.
        self._pending_runtime_swap = _swap
        if self._runtime_swap_task is None or self._runtime_swap_task.done():
            self._runtime_swap_task = asyncio.create_task(self._apply_pending_runtime_swap())
        logger.info("Runtime limits applied; provider/workspace swap deferred until the current turn ends")

    def _apply_runtime_limits(
        self,
        *,
        max_iterations: int,
        max_tokens: int,
        reasoning_effort: str | None,
        temperature: float | None | object = _KEEP,
        auto_max_rounds: int | None = None,  # used by ResearchAgentLoop
    ) -> None:
        """Knobs the loop reads on every call, so changing them mid-turn is safe."""
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        if temperature is not _KEEP:
            self.temperature = temperature
        subagents = getattr(self, "subagents", None)
        if subagents is not None:
            subagents.temperature = self.temperature
            subagents.max_tokens = self.max_tokens
            subagents.reasoning_effort = self.reasoning_effort

    async def _apply_pending_runtime_swap(self) -> None:
        while self._pending_runtime_swap is not None:
            async with self._processing_lock:
                swap, self._pending_runtime_swap = self._pending_runtime_swap, None
                if swap is None:
                    continue
                try:
                    await swap()
                except Exception:
                    # Deferred: no caller is waiting to receive the error.
                    logger.exception("Failed to apply the deferred runtime config update")

    async def _swap_runtime(
        self,
        *,
        provider: LLMProvider,
        model: str,
        provider_factory: Callable[[str], LLMProvider] | None,
        model_candidates: list[str] | None,
        role_provider_factory: "Callable[[str], tuple[LLMProvider, str, tuple[str, ...]]] | None",
        workspace: Path,
        restrict_to_workspace: bool,
        brave_api_key: str | None,
        web_proxy: str | None,
        exec_config: ExecToolConfig | None,
        knowledge_config: KnowledgeConfig | None,
        timezone: str | None,
        channels_config: ChannelsConfig | None,
        community_config: CommunityConfig | None,
        context_window_tokens: int | None,
    ) -> None:
        """Replace provider, workspace, tools and MCP connections (caller holds the lock)."""
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
        self.model_candidates = list(model_candidates) if model_candidates else None
        self.role_provider_factory = role_provider_factory
        self.workspace = next_workspace
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        if exec_config is not None:
            self.exec_config = exec_config
        if knowledge_config is not None:
            self.knowledge_config = knowledge_config
        self.timezone = timezone
        self.restrict_to_workspace = restrict_to_workspace
        if channels_config is not None:
            self.channels_config = channels_config
        if community_config is not None:
            self.community_config = community_config
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
        self.subagents.brave_api_key = self.brave_api_key
        self.subagents.web_proxy = self.web_proxy
        self.subagents.exec_config = self.exec_config
        self.subagents.restrict_to_workspace = self.restrict_to_workspace
        self.subagents.provider_factory = provider_factory
        self.subagents.model_candidates = self.model_candidates
        self.subagents.role_provider_factory = role_provider_factory
        self.subagents.runner = AgentRunner(provider)
        self.subagents._session_runtimes.clear()

        self.tools = ToolRegistry()
        self._register_default_tools()
        self._rebuild_memory_helpers()

        logger.info("Agent runtime reconfigured: model={}, workspace={}", self.model, self.workspace)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        skill_access_dirs = self._skill_access_dirs_for_workspace(self.workspace)

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
                sandbox=self.exec_config.sandbox or None,
                sandbox_network=self.exec_config.sandbox_network,
                sandbox_read_only_paths=self.exec_config.sandbox_read_only_paths,
                sandbox_extra_dirs=skill_access_dirs,
                background_registry=self._bg_registry,
                enable_background=True,
                python_runtime=self.exec_config.python,
            ))
            self.tools.register(BgTool(registry=self._bg_registry))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(PubMedSearchTool(proxy=self.web_proxy))
        self._knowledge_scope = KnowledgeScope() if self.knowledge_config.enabled else None
        if self._knowledge_scope is not None:
            self.tools.register(KnowledgeSearchTool(self._knowledge_scope))
            self.tools.register(KnowledgeReadTool(self._knowledge_scope))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SetPlanTool())
        self.tools.register(WorkflowInteractionTool())
        self.tools.register(InitiationResearchResultTool())
        self.tools.register(WorkflowArtifactTool())
        self.tools.register(WorkflowCompletionTool())
        self.tools.register(WorkflowExperimentTool())
        self.tools.register(SpawnTool(manager=self.subagents))
        self.tools.register(ConsultRoleTool(manager=self.subagents))
        if self.cron_service:
            cron_tool = CronTool(self.cron_service)
            setattr(cron_tool, "_default_timezone", self.timezone)
            self.tools.register(cron_tool)
        for community_tool in build_community_tools(self.community_config, workspace=self.workspace):
            self.tools.register(community_tool)

    def _skill_access_dirs_for_workspace(self, workspace: Path) -> list[Path]:
        if not (self.restrict_to_workspace or self.exec_config.sandbox):
            return []
        from mira_engine.agent.skills import skill_access_dirs

        return skill_access_dirs(workspace)

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
        workflow_run_id: str | None = None,
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
                if hasattr(tool, "set_workflow_run_context"):
                    tool.set_workflow_run_context(workflow_run_id)
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

        if session_key and (exec_tool := self.tools.get("exec")):
            if hasattr(exec_tool, "set_session_context"):
                exec_tool.set_session_context(session_key, message_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks and normalize native citation markup."""
        if not text:
            return None
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text)
        cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned)
        return normalize_citations(cleaned).strip() or None

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

    def _compose_extra_system(
        self,
        ui_system_instructions: object,
        guard_notice: object,
        channel: str | None = None,
    ) -> str | None:
        """Merge optional UI instructions, guardrail notices, a venv usage hint
        when ``tools.exec.python.manager`` is active, and — for community turns —
        the cached community rules the agent accepted on onboarding (#33)."""
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
        rules = ""
        if channel == "community":
            rules = (getattr(self.community_config, "rules_text", "") or "").strip()
        sections = [chunk for chunk in (python_hint, base, notice, rules) if chunk]
        return "\n\n".join(sections) if sections else None

    def _with_knowledge(
        self,
        extra_system: str | None,
        meta: dict[str, Any],
        project_ref: ProjectRef | None,
    ) -> str | None:
        """Scope the kb tools to this turn's knowledge bases and describe them."""
        scope = self._knowledge_scope
        if scope is None:
            return extra_system
        section = scope.begin_turn(meta, project_ref.project_dir if project_ref else None)
        return "\n\n".join(part for part in (extra_system, section) if part) or None

    def _get_model_runtime(self, session_key: str, *, role: str | None = None) -> RoutedProviderManager:
        """Return the session-local model runtime, creating it on demand."""
        if role and self.role_provider_factory is not None:
            # Keep role routes separate: switching profile must not reuse the
            # default route, nor change another concurrent project's provider.
            session_key = f"{session_key}:role:{role}"
        runtime = self._session_model_runtimes.get(session_key)
        if runtime is None:
            provider, model, candidates = self.provider, self.model, tuple(self.model_candidates or [self.model])
            provider_factory = self.provider_factory
            if role and self.role_provider_factory is not None:
                provider, model, candidates = self.role_provider_factory(role)
                # Role candidates belong to the role's explicit provider, not
                # the possibly different globally forced provider.
                provider_factory = None
            runtime = RoutedProviderManager(
                default_provider=provider,
                default_model=model,
                provider_factory=provider_factory,
                default_candidates=candidates,
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
        completion_guard: Callable[[str | None], Awaitable[str | None]] | None = None,
        turn_stop_guard: Callable[[], Awaitable[str | None]] | None = None,
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
        _final_content_streamed.set(False)
        set_turn_stop_reason(TURN_COMPLETED)

        while iteration < self.max_iterations:
            if turn_stop_guard is not None:
                stop_message = await turn_stop_guard()
                if stop_message is not None:
                    final_content = stop_message
                    messages = self.context.add_assistant_message(messages, stop_message)
                    set_turn_stop_reason(TURN_WAITING_INPUT)
                    break
            hook_ctx = AgentHookContext(iteration=iteration, messages=messages)
            if hook:
                await hook.before_iteration(hook_ctx)
            iteration += 1
            iteration_streamed = False

            use_routed_runtime = model_runtime is not None and (
                self.provider_factory is not None or self.role_provider_factory is not None
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
                    iteration_streamed = bool(clean_streamed)
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
                assert model_runtime is not None
                if active_provider is None or active_route is None:
                    active_provider, active_route = await model_runtime.resolve(messages, iteration)
                if on_stream is not None:
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

                    response, active_route = await model_runtime.chat_stream_with_retry(
                        active_route,
                        messages=messages,
                        tools=self.tools.get_definitions(),
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                        on_content_delta=_stream_delta,
                    )
                    clean_streamed = self._strip_think(streamed_raw)
                    iteration_streamed = bool(clean_streamed)
                    if response.content and clean_streamed:
                        response.content = clean_streamed
                    if on_stream_end:
                        await on_stream_end(resuming=False)
                else:
                    response, active_route = await model_runtime.chat(
                        active_route,
                        messages=messages,
                        tools=self.tools.get_definitions(),
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                        reasoning_effort=self.reasoning_effort,
                    )
            await record_token_usage(response.usage, getattr(active_route, "model", None) or self.model)
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
                    logger.info("Tool call: {} (arguments omitted)", tool_call.name)
                    tool_started = time.monotonic()
                    emit_event("tool.started", tool=tool_call.name)
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
                        manager = getattr(self, "subagents", None)
                        scope = manager.skill_audit_scope(audit_hook) if manager is not None else nullcontext()
                        with scope:
                            result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    except asyncio.CancelledError:
                        # /stop and shutdown cancel tools; they are not tool failures.
                        emit_event("tool.cancelled", tool=tool_call.name)
                        raise
                    except BaseException as exc:
                        emit_event("tool.failed", level="ERROR", tool=tool_call.name, exception=exc)
                        raise
                    finally:
                        emit_event("tool.finished", tool=tool_call.name,
                                   duration_ms=round((time.monotonic() - tool_started) * 1000, 2))
                        if ping_task is not None:
                            ping_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await ping_task
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                    if audit_hook:
                        loaded_event = skill_read_details(tool_call.name, tool_call.arguments, result)
                        if loaded_event:
                            await audit_hook(loaded_event)
                    hook_ctx.tool_results.append(result)
                    hook_ctx.tool_events.append(
                        {"name": tool_call.name, "status": "ok", "detail": str(result)}
                    )
                # Finish the batch's tool-result protocol, but never ask the model
                # to continue after a durable user-input/control boundary.
                if turn_stop_guard is not None:
                    stop_message = await turn_stop_guard()
                    if stop_message is not None:
                        final_content = stop_message
                        messages = self.context.add_assistant_message(messages, stop_message)
                        set_turn_stop_reason(TURN_WAITING_INPUT)
                        hook_ctx.final_content = stop_message
                        hook_ctx.stop_reason = "durable_boundary"
                        if hook:
                            await hook.after_iteration(hook_ctx)
                        break
            else:
                clean = self._strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error("LLM returned an error (response content omitted)")
                    emit_event("provider.failed", level="ERROR", reason="error_finish_reason")
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    # Save a neutral placeholder so the session doesn't end
                    # with an orphaned user message (consecutive users cause
                    # permanent 400 loops with strict providers like Anthropic).
                    messages = self.context.add_assistant_message(
                        messages, "(error — see previous log)"
                    )
                    set_turn_stop_reason(TURN_ERROR)
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
                if completion_guard is not None and iteration < self.max_iterations:
                    continuation = await completion_guard(clean)
                    if continuation:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"{self._AUTO_CONTINUE_MARKER}\n"
                                    f"{continuation}"
                                ),
                            }
                        )
                        hook_ctx.final_content = None
                        hook_ctx.stop_reason = "contract_continuation"
                        if hook:
                            await hook.after_iteration(hook_ctx)
                        continue
                final_content = clean
                _final_content_streamed.set(iteration_streamed)
                # "length": the reply was cut off by the output token limit.
                set_turn_stop_reason(
                    TURN_MAX_TOKENS if response.finish_reason == "length" else TURN_COMPLETED
                )
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
                f"I stopped before finishing: this turn reached the limit of "
                f"{self.max_iterations} tool calls. Reply \"continue\" and I will pick up "
                "where I left off, or break the task into smaller steps."
            )
            # Close the turn in the transcript too, so the saved session does not
            # end on a bare tool result and the next message reads as a follow-up.
            messages = self.context.add_assistant_message(messages, final_content)
            set_turn_stop_reason(TURN_MAX_ITERATIONS)

        self._last_loop_tokens_used = loop_tokens_used
        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        await self._warm_skill_scan_cache()
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
                task.add_done_callback(self._make_dispatch_done_callback(msg, effective_key))

    def _make_dispatch_done_callback(
        self, msg: InboundMessage, effective_key: str
    ):
        """Drop finished tasks and publish a CLI-facing error if dispatch crashed."""

        def _callback(task: asyncio.Task) -> None:
            active = self._active_tasks.get(effective_key, [])
            if task in active:
                active.remove(task)
            if task.cancelled():
                return
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                return
            if exc is None:
                return
            logger.opt(exception=exc).error(
                "Unhandled error in dispatch for session {}", msg.session_key
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(self._publish_dispatch_failure(msg, exc))

        return _callback

    def _build_error_outbound(
        self, msg: InboundMessage, exc: BaseException
    ) -> OutboundMessage:
        """Build a user-facing error reply that explains *exc* concisely.

        The English ``content`` serves channels without their own i18n (CLI,
        Telegram, ...). The structured ``error_code``/``error_detail`` metadata
        lets the UI localize the message via its own translation layer.
        """
        from mira_engine.utils.runtime import (
            build_dispatch_error_text,
            classify_exception,
            error_detail_text,
        )

        metadata = dict(msg.metadata or {})
        metadata["_error"] = True
        metadata["error_type"] = type(exc).__name__
        metadata["error_code"] = classify_exception(exc)
        metadata["error_detail"] = error_detail_text(exc)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=build_dispatch_error_text(exc, channel=msg.channel),
            metadata=metadata,
        )

    async def _publish_dispatch_failure(
        self, msg: InboundMessage, exc: BaseException
    ) -> None:
        await self.bus.publish_outbound(self._build_error_outbound(msg, exc))

    async def _handle_control(self, msg: InboundMessage, control: str) -> bool:
        """Hook for subclasses to handle ``_control`` metadata messages.

        The base loop has no control messages of its own and always returns
        ``False``. Subclasses (notably :class:`ResearchAgentLoop`) override
        this to handle entries such as ``set_mode`` without forking ``run``.
        """
        return False

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        metadata = msg.metadata or {}
        # /stop bypasses _dispatch, so it needs its own correlation scope.
        with diagnostic_context(request_id=correlation_id(metadata.get("_diagnostic_request_id")),
                                session_ref=opaque_ref(msg.session_key), channel=msg.channel):
            emit_event("agent.stop.requested")
            total = await self._handle_stop_with_context(msg)
            emit_event("agent.stop.completed", count=total)

    async def _handle_stop_with_context(self, msg: InboundMessage) -> int:
        project_ref = self._project_ref_from_metadata(msg.metadata)
        raw_key = (
            UNIFIED_SESSION_KEY
            if getattr(self, "_unified_session", False) and not msg.session_key_override
            else msg.session_key
        )
        scoped_key = self._scoped_session_key(project_ref, raw_key)
        cancelled, sub_cancelled, background_cancelled = await self._cancel_session_work(
            scoped_key
        )
        total = cancelled + sub_cancelled + background_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        metadata = dict(msg.metadata or {})
        metadata.update({
            "_stop_ack": True,
            "request_id": metadata.get("_stop_request_id"),
            "cancelled_agent_tasks": cancelled,
            "cancelled_subagents": sub_cancelled,
            "cancelled_background_jobs": background_cancelled,
        })
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
            metadata=metadata,
        ))
        return total

    async def _cancel_session_work(self, scoped_key: str) -> tuple[int, int, int]:
        """Cancel agent, subagent and background work owned by a session."""
        tasks = self._active_tasks.pop(scoped_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(scoped_key)
        registry = getattr(self, "_bg_registry", None)
        background_cancelled = (
            await registry.kill_by_session(scoped_key) if registry is not None else 0
        )
        return cancelled, sub_cancelled, background_cancelled

    async def _dispatch(self, msg: InboundMessage) -> None:
        metadata = msg.metadata or {}
        request_id = correlation_id(metadata.get("_diagnostic_request_id"))
        started = time.monotonic()
        with diagnostic_context(request_id=request_id, run_id=correlation_id(),
                                session_ref=opaque_ref(msg.session_key), channel=msg.channel,
                                workflow_run_id=metadata.get("workflow_run_id")):
            emit_event("agent.dispatch.started")
            # Only the UI renders turn state; other channels would print the
            # empty lifecycle messages.
            lifecycle = msg.channel == "ui"
            turn_id = metadata.get("turn_id") if isinstance(metadata.get("turn_id"), str) else None
            turn_id = turn_id or f"turn-{uuid.uuid4().hex[:12]}"
            if lifecycle:
                await self._publish_turn_event(msg, turn_id, "started")
            set_turn_stop_reason(TURN_COMPLETED)
            stop_reason: str | None = None
            try:
                await self._dispatch_with_context(msg)
            except asyncio.CancelledError:
                stop_reason = TURN_STOPPED
                emit_event("agent.dispatch.cancelled")
                raise
            except Exception as exc:
                stop_reason = TURN_ERROR
                emit_event("agent.dispatch.failed", level="ERROR", exception=exc)
                raise
            finally:
                emit_event("agent.dispatch.finished",
                           duration_ms=round((time.monotonic() - started) * 1000, 2))
                # Exactly one turn_finished per turn, however it ended, so the
                # client never keeps a "thinking" state for a finished turn.
                if lifecycle:
                    await self._publish_turn_event(
                        msg, turn_id, "finished", stop_reason or _turn_stop_reason.get(),
                    )

    async def _publish_turn_event(
        self,
        msg: InboundMessage,
        turn_id: str,
        event: str,
        stop_reason: str | None = None,
    ) -> None:
        metadata = dict(msg.metadata or {})
        metadata.update({"_turn_event": event, "turn_id": turn_id})
        if stop_reason is not None:
            metadata["stop_reason"] = stop_reason
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content="", metadata=metadata,
        ))

    async def _dispatch_with_context(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        if getattr(self, "_unified_session", False) and not msg.session_key_override:
            msg.session_key_override = UNIFIED_SESSION_KEY

        if bool((msg.metadata or {}).get("_wants_stream")):
            stream_meta = dict(msg.metadata or {})
            _final_content_streamed.set(False)
            streamed_any = False
            fallback_prefix = "MIRA_SKILL_FALLBACK_REQUIRED:"
            inspect_fallback_prefix = bool(stream_meta.get("selected_skill_ids"))
            pending_prefix = ""
            fallback_detected = False

            async def _on_stream(delta: str) -> None:
                nonlocal streamed_any, inspect_fallback_prefix, pending_prefix, fallback_detected
                if not delta:
                    return
                if fallback_detected:
                    pending_prefix += delta
                    return
                if inspect_fallback_prefix:
                    pending_prefix += delta
                    candidate = pending_prefix.lstrip()
                    if candidate == fallback_prefix:
                        fallback_detected = True
                        return
                    if fallback_prefix.startswith(candidate):
                        return
                    if candidate.startswith(fallback_prefix):
                        fallback_detected = True
                        return
                    delta = pending_prefix
                    pending_prefix = ""
                    inspect_fallback_prefix = False
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
                nonlocal streamed_any, pending_prefix
                if resuming:
                    return
                if pending_prefix and not fallback_detected:
                    streamed_any = True
                    meta = dict(stream_meta)
                    meta["_stream_delta"] = True
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=pending_prefix,
                        metadata=meta,
                    ))
                    pending_prefix = ""
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
                # Publish the final response unless its text already reached the
                # client as deltas: the runtime may never have streamed (e.g. a
                # routed provider without token streaming), or the turn ended
                # on text the model never produced (iteration limit, a stop
                # guard, an error), and without it the client keeps "thinking".
                if response is not None and (not streamed_any or not _final_content_streamed.get()):
                    await self.bus.publish_outbound(response)
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except KeyboardInterrupt:
                raise asyncio.CancelledError() from None
            except SystemExit:
                raise asyncio.CancelledError() from None
            except Exception as exc:
                logger.exception("Error processing message for session {}", msg.session_key)
                emit_event("agent.turn.failed", level="ERROR", exception=exc)
                set_turn_stop_reason(TURN_ERROR)
                await self.bus.publish_outbound(self._build_error_outbound(msg, exc))
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
            except KeyboardInterrupt:
                raise asyncio.CancelledError() from None
            except SystemExit:
                raise asyncio.CancelledError() from None
            except Exception as exc:
                logger.exception("Error processing message for session {}", msg.session_key)
                emit_event("agent.turn.failed", level="ERROR", exception=exc)
                set_turn_stop_reason(TURN_ERROR)
                await self.bus.publish_outbound(self._build_error_outbound(msg, exc))

    async def _warm_skill_scan_cache(self) -> None:
        """Pre-scan built-in skills off the event loop so Ctrl+C stays responsive."""
        skills = getattr(getattr(self, "context", None), "skills", None)
        plugin_manager = getattr(skills, "plugin_manager", None) if skills else None
        if plugin_manager is None:
            return
        try:
            await asyncio.to_thread(plugin_manager.warm_builtin_manifest_cache)
        except Exception:
            logger.debug("Skill manifest warmup failed", exc_info=True)

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
        self._session_model_runtimes.pop(f"{session_key}:role:supervisor", None)

    @track_token_usage
    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        completion_guard: Callable[[str | None], Awaitable[str | None]] | None = None,
        turn_stop_guard: Callable[[], Awaitable[str | None]] | None = None,
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

        logger.info("Processing message on {} (content length={})", msg.channel, len(msg.content))

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
            meta.get("turn_id") or meta.get("message_id"),
            project_ref=project_ref,
            session_key=key,
            workflow_run_id=meta.get("workflow_run_id")
            if isinstance(meta.get("workflow_run_id"), str)
            else None,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # Community threads are public, untrusted input: never archive them into
        # the long-term memory that Dream distils into USER.md/MEMORY.md
        # (mira#203). Their sessions stay bounded by ``memory_window``.
        if msg.channel != COMMUNITY_CHANNEL:
            await project_consolidator.maybe_consolidate_by_tokens(session)
        history = session.get_history(max_messages=self.memory_window)
        profile = meta.get("agent_profile") or getattr(self, "_session_agent_profiles", {}).get(key)
        model_runtime = self._get_model_runtime(key, role="supervisor" if profile == "team" else None)
        extra_system = self._compose_extra_system(
            meta.get("_ui_system_instructions"),
            meta.get("_task_plan_guard_notice"),
            channel=getattr(msg, "channel", None),
        )
        extra_system = self._with_knowledge(extra_system, meta, project_ref)

        raw_selected_skills = meta.get("selected_skill_ids")
        selected_skills = [
            name for name in (
                str(item).strip() for item in raw_selected_skills
                if isinstance(item, str)
            )
            if name and ctx.skills.load_skill(name)
        ] if isinstance(raw_selected_skills, list) else []

        suggested_skills = ctx.skills.suggest_skills(
            msg.content,
            recent=recent_skill_names,
            limit=3,
        )
        active_skills: list[str] = []
        for name in [*selected_skills, *suggested_skills]:
            if name not in active_skills:
                active_skills.append(name)
        active_skills = active_skills[:8]
        skill_hint = ""
        if selected_skills:
            fallback_instruction = (
                "The user approved the previously proposed fallback for this turn. "
                "Deviate only as narrowly as that proposal requires."
                if meta.get("_skill_fallback_approved")
                else "If their workflow cannot continue, stop using tools and reply with "
                     "`MIRA_SKILL_FALLBACK_REQUIRED:` followed by the exact error and proposed "
                     "fallback. Wait for explicit user approval before deviating."
            )
            skill_hint = (
                "User-selected skills are mandatory system-level instructions for this turn:\n"
                + "\n".join(f"- {name}" for name in selected_skills)
                + "\nFollow their documented workflow and recommended calls. Do not bypass them "
                  "by inspecting implementation source, calling internal functions, or writing a "
                  "replacement implementation. " + fallback_instruction
            )
        elif suggested_skills:
            skill_hint = (
                "Skill routing selected these active skills. Their full instructions are included "
                "in the system prompt; prefer their documented workflow when relevant:\n"
                + "\n".join(f"- {name}" for name in suggested_skills)
            )
        if on_progress and (selected_skills or suggested_skills):
            try:
                await on_progress(
                    f"skill -> {', '.join(selected_skills or suggested_skills)}",
                    tool_hint=True,
                )
            except TypeError:
                await on_progress(f"skill -> {', '.join(selected_skills or suggested_skills)}")
        if extra_system:
            extra_system = skill_hint + "\n\n" + extra_system if skill_hint else extra_system
        else:
            extra_system = skill_hint or None
        loaded_skills: list[str] = []
        initial_messages = ctx.build_messages(
            history=history,
            current_message=msg.content,
            skill_names=active_skills or None,
            loaded_skills=loaded_skills,
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
        loaded_this_turn: set[tuple[str | None, str]] = set()
        audit_cb = None
        emit_audit_to_channel = msg.channel == "ui" or bool(meta.get("_emit_skill_audit"))
        if emit_audit_to_channel or audit_hook:
            async def _audit(details: dict[str, Any]) -> None:
                if details.get("loaded"):
                    identity = (details.get("agent"), details.get("skill_name"))
                    if identity in loaded_this_turn:
                        return
                    loaded_this_turn.add(identity)
                audit_context = meta.get("_audit_context")
                enriched_details = {
                    **(audit_context if isinstance(audit_context, dict) else {}),
                    **details,
                }
                skill_name = enriched_details.get("skill_name")
                if not details.get("loaded") and isinstance(skill_name, str) and skill_name.strip():
                    current_turn_skills.add(skill_name.strip())
                if audit_hook:
                    await audit_hook(enriched_details)
                if not emit_audit_to_channel:
                    return
                metadata = dict(msg.metadata or {})
                metadata["_audit_only"] = True
                metadata["_audit_event"] = (
                    "skill_loaded" if details.get("loaded") else
                    "skill_selected" if details.get("selected") else "skill_invoked"
                )
                metadata["_audit_details"] = enriched_details
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata=metadata,
                ))

            audit_cb = _audit
        if audit_cb:
            for name in loaded_skills:
                source = "manual" if name in selected_skills else "auto" if name in suggested_skills else "recent" if name in recent_skill_names else "always"
                await audit_cb(skill_loaded(name, source, tool="system_prompt"))
            for selected_skill in selected_skills:
                await audit_cb({
                    "tool": "system_prompt",
                    "skill_name": selected_skill,
                    "selected": True,
                })
        run_kwargs: dict[str, Any] = {
            "model_runtime": model_runtime,
            "on_progress": progress_cb,
            "audit_hook": audit_cb,
            "completion_guard": completion_guard,
        }
        if turn_stop_guard is not None:
            run_kwargs["turn_stop_guard"] = turn_stop_guard
        if on_stream is not None:
            run_kwargs["on_stream"] = on_stream
        if on_stream_end is not None:
            run_kwargs["on_stream_end"] = on_stream_end
        await self._emit_activity_ping(progress_cb)
        final_content, _, all_msgs = await self._run_agent_loop(initial_messages, **run_kwargs)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        if isinstance(session.metadata, dict):
            merged: list[str] = []
            # Remember this task's skills, not an ever-growing list of old topics.
            for item in [*(name for name in active_skills if name in loaded_skills), *sorted(current_turn_skills)]:
                if item not in merged:
                    merged.append(item)
            session.metadata["_recent_skills"] = merged[-10:]

        self._save_turn(session, all_msgs, 1 + len(history))
        sessions_mgr.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        logger.info("Response on {} (content length={})", msg.channel, len(final_content))
        outbound_meta = dict(msg.metadata or {})
        fallback_prefix = "MIRA_SKILL_FALLBACK_REQUIRED:"
        if selected_skills and final_content.lstrip().startswith(fallback_prefix):
            final_content = final_content.lstrip()[len(fallback_prefix):].lstrip()
            outbound_meta["_skill_fallback_required"] = True
            outbound_meta["selected_skill_ids"] = selected_skills
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=outbound_meta,
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
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            metadata=dict(metadata or {}),
        )
        with diagnostic_context(run_id=correlation_id(), session_ref=opaque_ref(session_key), channel=channel):
            emit_event("agent.direct.started")
            started = time.monotonic()
            try:
                await self._connect_mcp()
                response = await self._process_message(
                    msg,
                    session_key=session_key,
                    on_progress=on_progress,
                    audit_hook=audit_hook,
                )
            except asyncio.CancelledError:
                emit_event("agent.direct.cancelled")
                raise
            except Exception as exc:
                emit_event("agent.direct.failed", level="ERROR", exception=exc)
                raise
            finally:
                emit_event("agent.direct.finished",
                           duration_ms=round((time.monotonic() - started) * 1000, 2))
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
