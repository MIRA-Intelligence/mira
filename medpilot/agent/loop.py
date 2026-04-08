"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from medpilot.agent.context import ContextBuilder
from medpilot.agent.memory import MemoryStore
from medpilot.agent.routing import ModelRouter, RoutedProviderManager
from medpilot.agent.subagent import SubagentManager
from medpilot.agent.tools.cron import CronTool
from medpilot.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from medpilot.agent.tools.message import MessageTool
from medpilot.agent.tools.registry import ToolRegistry
from medpilot.agent.tools.shell import ExecTool
from medpilot.agent.tools.spawn import SpawnTool
from medpilot.agent.tools.web import WebFetchTool, WebSearchTool
from medpilot.bus.events import InboundMessage, OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.providers.base import LLMProvider
from medpilot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from medpilot.config.schema import ChannelsConfig, ExecToolConfig
    from medpilot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 500
    _DEFAULT_AUTO_MAX_ROUNDS = 30
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
        auto_max_rounds: int = _DEFAULT_AUTO_MAX_ROUNDS,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        provider_factory: Callable[[str], LLMProvider] | None = None,
        model_router: ModelRouter | None = None,
    ):
        from medpilot.config.schema import ExecToolConfig
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
        self.auto_max_rounds = auto_max_rounds
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self._project_sessions: dict[str, SessionManager] = {}
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

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._session_run_modes: dict[str, str] = {}  # session_key -> manual|auto
        self._session_auto_max_rounds: dict[str, int] = {}  # session_key -> per-session auto round cap
        self._session_agent_profiles: dict[str, str] = {}  # session_key -> engineer|default|research
        self._processing_lock = asyncio.Lock()
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from medpilot.agent.tools.mcp import connect_mcp_servers
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

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
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

    @staticmethod
    def _normalize_run_mode(value: object) -> str:
        """Normalize UI run mode with a conservative fallback."""
        if isinstance(value, str):
            mode = value.strip().lower()
            if mode in {"manual", "auto"}:
                return mode
        return "manual"

    @staticmethod
    def _parse_run_mode(value: object) -> str | None:
        """Parse run mode, returning None when absent/invalid."""
        if isinstance(value, str):
            mode = value.strip().lower()
            if mode in {"manual", "auto"}:
                return mode
        return None

    @staticmethod
    def _parse_agent_profile(value: object) -> str | None:
        """Parse agent profile, returning None when absent/invalid."""
        if isinstance(value, str):
            profile = value.strip().lower()
            if profile in {"engineer", "default", "research"}:
                return profile
        return None

    @staticmethod
    def _parse_auto_max_rounds(value: object) -> int | None:
        """Parse auto max rounds, returning None when absent/invalid."""
        if isinstance(value, bool):  # bool is int-like in Python; reject explicitly
            return None
        if isinstance(value, int):
            return value if value >= 1 else None
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            try:
                parsed = int(raw)
            except ValueError:
                return None
            return parsed if parsed >= 1 else None
        return None

    def _resolve_session_run_mode(self, session_key: str, inbound_value: object) -> str:
        """Resolve effective mode for a session, updating cache if explicitly provided."""
        explicit = self._parse_run_mode(inbound_value)
        if explicit:
            self._session_run_modes[session_key] = explicit
            return explicit
        return self._session_run_modes.get(session_key, "manual")

    def _resolve_session_auto_max_rounds(self, session_key: str, inbound_value: object) -> int:
        """Resolve effective auto round cap, updating cache if explicitly provided."""
        explicit = self._parse_auto_max_rounds(inbound_value)
        if explicit is not None:
            self._session_auto_max_rounds[session_key] = explicit
            return explicit
        return self._session_auto_max_rounds.get(session_key, self.auto_max_rounds)

    def _resolve_session_agent_profile(self, session_key: str, inbound_value: object) -> str:
        """Resolve effective agent profile, updating cache if explicitly provided."""
        explicit = self._parse_agent_profile(inbound_value)
        if explicit:
            self._session_agent_profiles[session_key] = explicit
            return explicit
        return self._session_agent_profiles.get(session_key, "default")

    @staticmethod
    def _agent_profile_to_agents_filename(profile: str) -> str:
        """Map profile to its AGENTS bootstrap file."""
        if profile == "engineer":
            return "AGENTS_EG.md"
        if profile == "research":
            return "AGENTS_RS.md"
        return "AGENTS.md"

    @staticmethod
    def _looks_like_user_input_request(text: str | None) -> bool:
        """Heuristic: detect when assistant explicitly needs user input."""
        if not text:
            return False
        lowered = text.lower()
        keywords = (
            "please provide",
            "please confirm",
            "please choose",
            "could you",
            "can you provide",
            "which option",
            "clarify",
            "need your input",
            "what would you like to do next",
            "需要你",
            "请提供",
            "请确认",
            "请选择",
            "是否继续",
            "是否开始",
            "是否要我",
            "要我现在",
        )
        return any(k in lowered for k in keywords)

    @staticmethod
    def _looks_like_failure_response(text: str | None) -> bool:
        """Heuristic: detect blocking errors where auto should stop.

        IMPORTANT: Do not treat ordinary experiment outcomes like "hypothesis failed"
        as blocking failures. We only stop on explicit runtime/system blockage.
        """
        if not text:
            return False
        lowered = text.lower()
        hard_signals = (
            "traceback (most recent call last)",
            "sorry, i encountered an error",
            "memory archival failed",
            "command timed out",
            "exit code:",
            "permission denied",
            "no such file or directory",
            "module not found",
            "failed to connect",
            "failed to load",
            "tool call failed",
            "unrecoverable",
            "blocked by",
            "无法继续",
            "出现错误",
            "运行时错误",
        )
        if any(k in lowered for k in hard_signals):
            return True
        if lowered.startswith("error:") or "\nerror:" in lowered:
            return True
        return False

    @staticmethod
    def _load_task_plan(project_dir: str | None) -> dict | None:
        """Load web task_plan.json if available."""
        if not project_dir:
            return None
        plan_path = Path(project_dir) / "task_plan.json"
        if not plan_path.is_file():
            return None
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _plan_has_pending_work(plan: dict | None) -> bool:
        """Return whether task_plan still has pending/running experiments."""
        if not plan:
            return False
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return False
        return any(
            isinstance(exp, dict) and exp.get("status") in {"pending", "running"}
            for exp in experiments
        )

    def _build_auto_continue_message(
        self,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
    ) -> str:
        """Build the synthetic internal continue message for server-side auto mode."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Continue automatically to the next pending experiment or stage. "
            "Do not stop for confirmation unless user input is strictly required."
        )

    def _should_continue_auto_web(
        self,
        *,
        channel: str,
        run_mode: str,
        auto_max_rounds: int,
        project_dir: str | None,
        final_content: str | None,
        auto_round: int,
    ) -> bool:
        """Decide whether to schedule another internal auto-run cycle."""
        if channel != "web" or run_mode != "auto":
            return False
        if auto_round >= auto_max_rounds:
            logger.warning("Auto mode max rounds ({}) reached", auto_max_rounds)
            return False
        if self._looks_like_failure_response(final_content):
            return False
        if self._looks_like_user_input_request(final_content):
            return False
        plan = self._load_task_plan(project_dir)
        return self._plan_has_pending_work(plan)

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
        model_runtime: RoutedProviderManager,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        active_provider: LLMProvider | None = None
        active_route = None

        while iteration < self.max_iterations:
            iteration += 1

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

            if iteration == 1 and on_progress and self.model_router and self.model_router.enabled:
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
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
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
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

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
            if control == "set_mode":
                await self._handle_set_mode(msg)
            elif msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _handle_set_mode(self, msg: InboundMessage) -> None:
        """Update session run mode immediately without entering normal dispatch."""
        mode = self._normalize_run_mode((msg.metadata or {}).get("run_mode"))
        self._session_run_modes[msg.session_key] = mode
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"Run mode switched to {mode}.",
            metadata={
                "_control": "set_mode_ack",
                "run_mode": mode,
            },
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
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
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            model_runtime = self._get_model_runtime(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages, model_runtime=model_runtime)
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = msg.metadata or {}
        project_dir = meta.get("project_dir")
        key = session_key or msg.session_key
        run_mode = self._resolve_session_run_mode(key, meta.get("run_mode"))
        auto_max_rounds = self._resolve_session_auto_max_rounds(key, meta.get("auto_max_rounds"))
        agent_profile = self._resolve_session_agent_profile(key, meta.get("agent_profile"))
        agents_filename = self._agent_profile_to_agents_filename(agent_profile)
        if project_dir:
            sessions_mgr = self._get_project_sessions(project_dir)
        else:
            sessions_mgr = self.sessions

        session = sessions_mgr.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            _mw = Path(project_dir) if project_dir else self.workspace
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True, workspace_override=_mw):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)

            session.clear()
            sessions_mgr.save(session)
            sessions_mgr.invalidate(session.key)
            self._session_model_runtimes.pop(session.key, None)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 medpilot commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/help — Show available commands")

        memory_workspace = Path(project_dir) if project_dir else self.workspace

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            _mw = memory_workspace

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session, workspace_override=_mw)
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.memory_window)
        model_runtime = self._get_model_runtime(key)
        extra_system = meta.get("_ui_system_instructions")

        ctx = ContextBuilder(memory_workspace) if project_dir else self.context
        initial_messages = ctx.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            project_dir=project_dir,
            run_mode=run_mode,
            agents_filename=agents_filename,
            extra_system=extra_system,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        progress_cb = on_progress or _bus_progress
        audit_cb = None
        if msg.channel == "web":
            async def _web_audit(details: dict[str, Any]) -> None:
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

            audit_cb = _web_audit
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            model_runtime=model_runtime,
            on_progress=progress_cb,
            audit_hook=audit_cb,
        )

        auto_round = 0
        while True:
            current_mode = self._session_run_modes.get(key, run_mode)
            current_auto_max_rounds = self._session_auto_max_rounds.get(key, auto_max_rounds)
            if not self._should_continue_auto_web(
                channel=msg.channel,
                run_mode=current_mode,
                auto_max_rounds=current_auto_max_rounds,
                project_dir=project_dir,
                final_content=final_content,
                auto_round=auto_round,
            ):
                break
            run_mode = current_mode
            auto_round += 1
            await progress_cb(
                f"auto-run round {auto_round}: continuing to next pending experiment"
            )
            all_msgs.append({
                "role": "user",
                "content": self._build_auto_continue_message(
                    msg.channel,
                    msg.chat_id,
                    project_dir,
                    run_mode,
                ),
            })
            final_content, _, all_msgs = await self._run_agent_loop(
                all_msgs,
                model_runtime=model_runtime,
                on_progress=progress_cb,
                audit_hook=audit_cb,
            )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        sessions_mgr.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
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
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    def _get_project_sessions(self, project_dir: str) -> SessionManager:
        """Return a per-project SessionManager, creating one if needed."""
        if project_dir not in self._project_sessions:
            self._project_sessions[project_dir] = SessionManager(Path(project_dir))
        return self._project_sessions[project_dir]

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
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
