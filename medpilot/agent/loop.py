"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from medpilot.command.router import CommandContext, CommandRouter
from medpilot.agent.context import ContextBuilder
from medpilot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from medpilot.agent.memory import Consolidator, Dream, MemoryStore
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
from medpilot.agent.tools.search import GlobTool, GrepTool
from medpilot.agent.tools.web import WebFetchTool, WebSearchTool
from medpilot.bus.events import InboundMessage, OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.providers.base import LLMProvider
from medpilot.session.manager import Session, SessionManager
from medpilot.task_plan.guardrails import get_task_plan_contract, guard_task_plan_file

if TYPE_CHECKING:
    from medpilot.config.schema import ChannelsConfig, ExecToolConfig
    from medpilot.cron.service import CronService

UNIFIED_SESSION_KEY = "unified:default"


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
    _RUNTIME_CHECKPOINT_KEY = "_runtime_checkpoint"
    _AUTO_MAX_ROUNDS = 20
    _AUTO_GUARD_REPAIR_MAX = 1
    _AUTO_CHECKPOINT_REPAIR_MAX = 1
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
        self._session_run_modes: dict[str, str] = {}  # session_key -> manual|auto
        self._session_agent_profiles: dict[str, str] = {}  # session_key -> engineer|default|research
        self._session_automation_policies: dict[str, dict[str, Any] | None] = {}
        self._last_task_plan_guard_issues: list[str] = []
        self._last_task_plan_guard_fixed: bool = False
        self._last_loop_tokens_used: int = 0
        self._processing_lock = asyncio.Lock()
        self._register_default_tools()
        self._command_router = CommandRouter()
        from medpilot.command.builtin import register_builtin_commands

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

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        skill_access_dirs: list[Path] = []
        if self.restrict_to_workspace:
            from medpilot.agent.skills import SkillsLoader

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

        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=skill_access_dirs))
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
            ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            cron_tool = CronTool(self.cron_service)
            setattr(cron_tool, "_default_timezone", self.timezone)
            self.tools.register(cron_tool)

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
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text)
        cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned)
        return cleaned.strip() or None

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
    def _parse_automation_policy(value: object) -> dict[str, Any] | None:
        """Parse automation policy, returning None when absent/invalid."""
        if not isinstance(value, dict):
            return None

        logic_raw = value.get("logic")
        logic = "AND"
        if isinstance(logic_raw, str) and logic_raw.strip().upper() in {"AND", "OR"}:
            logic = logic_raw.strip().upper()

        goals: list[dict[str, Any]] = []
        raw_goals = value.get("goals")
        if isinstance(raw_goals, list):
            for item in raw_goals:
                if not isinstance(item, dict):
                    continue
                metric = item.get("metric")
                operator = item.get("operator")
                raw_target = item.get("value")
                if not isinstance(metric, str) or not metric.strip():
                    continue
                if not isinstance(operator, str) or operator not in {">", ">=", "<", "<=", "=="}:
                    continue
                try:
                    target = float(raw_target)
                except (TypeError, ValueError):
                    continue
                goals.append({
                    "metric": metric.strip(),
                    "operator": operator,
                    "value": target,
                })

        max_experiments = value.get("maxExperiments")
        if not isinstance(max_experiments, int) or max_experiments <= 0:
            max_experiments = None

        max_tokens = value.get("maxTokens")
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            max_tokens = None

        if not goals and max_experiments is None and max_tokens is None:
            return None

        parsed: dict[str, Any] = {"logic": logic, "goals": goals}
        if max_experiments is not None:
            parsed["maxExperiments"] = max_experiments
        if max_tokens is not None:
            parsed["maxTokens"] = max_tokens
        return parsed

    def _resolve_session_run_mode(self, session_key: str, inbound_value: object) -> str:
        """Resolve effective mode for a session, updating cache if explicitly provided."""
        explicit = self._parse_run_mode(inbound_value)
        if explicit:
            self._session_run_modes[session_key] = explicit
            return explicit
        return self._session_run_modes.get(session_key, "manual")

    def _resolve_session_agent_profile(self, session_key: str, inbound_value: object) -> str:
        """Resolve effective agent profile, updating cache if explicitly provided."""
        explicit = self._parse_agent_profile(inbound_value)
        if explicit:
            self._session_agent_profiles[session_key] = explicit
            return explicit
        return self._session_agent_profiles.get(session_key, "default")

    def _resolve_session_automation_policy(
        self,
        session_key: str,
        inbound_value: object,
    ) -> dict[str, Any] | None:
        """Resolve automation policy for a session, updating cache when provided."""
        if inbound_value is not None:
            parsed = self._parse_automation_policy(inbound_value)
            self._session_automation_policies[session_key] = parsed
            return parsed
        return self._session_automation_policies.get(session_key)

    @staticmethod
    def _agent_profile_to_agents_filename(profile: str) -> str:
        """Map profile to its AGENTS bootstrap file."""
        if profile == "engineer":
            return "AGENTS_EG.md"
        if profile == "research":
            return "AGENTS_RS.md"
        return "AGENTS.md"

    @staticmethod
    def _compose_extra_system(
        ui_system_instructions: object,
        guard_notice: object,
    ) -> str | None:
        """Merge optional UI instructions with guardrail notices."""
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
        if base and notice:
            return f"{base}\n\n{notice}"
        return base or notice or None

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

    @staticmethod
    def _plan_experiment_index(plan: dict | None) -> dict[str, dict[str, Any]]:
        """Build experiment lookup by id from task_plan payload."""
        if not plan:
            return {}
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return {}

        index: dict[str, dict[str, Any]] = {}
        for exp in experiments:
            if not isinstance(exp, dict):
                continue
            exp_id = exp.get("id")
            if not isinstance(exp_id, str):
                continue
            normalized = exp_id.strip()
            if not normalized:
                continue
            index[normalized] = exp
        return index

    @staticmethod
    def _running_experiment_ids(plan: dict | None) -> list[str]:
        """Return ids of currently running experiments."""
        if not plan:
            return []
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return []

        ids: list[str] = []
        for exp in experiments:
            if not isinstance(exp, dict) or exp.get("status") != "running":
                continue
            exp_id = exp.get("id")
            if isinstance(exp_id, str) and exp_id.strip():
                ids.append(exp_id.strip())
        return ids

    @classmethod
    def _has_experiment_checkpoint_update(
        cls,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> bool:
        """Check whether running experiments were persisted in task_plan this round."""
        running_ids = cls._running_experiment_ids(before_plan)
        if not running_ids:
            return True

        before_index = cls._plan_experiment_index(before_plan)
        after_index = cls._plan_experiment_index(after_plan)

        for exp_id in running_ids:
            before_entry = before_index.get(exp_id)
            after_entry = after_index.get(exp_id)
            # Entry disappeared or changed => task plan checkpoint advanced.
            if after_entry is None:
                return True
            if before_entry != after_entry:
                return True
        return False

    @classmethod
    def _experiments_crossed_boundary(
        cls,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> list[str]:
        """Return experiment ids that moved from active to terminal in one round."""
        before_index = cls._plan_experiment_index(before_plan)
        after_index = cls._plan_experiment_index(after_plan)
        terminal_statuses = {"completed", "failed", "skipped"}
        active_statuses = {"pending", "running"}

        crossed: list[str] = []
        for exp_id, after_entry in after_index.items():
            if not isinstance(after_entry, dict):
                continue
            after_status = after_entry.get("status")
            if after_status not in terminal_statuses:
                continue
            before_entry = before_index.get(exp_id)
            before_status = before_entry.get("status") if isinstance(before_entry, dict) else None
            if before_status in active_statuses or before_status is None:
                crossed.append(exp_id)
        return crossed
    @staticmethod
    def _to_number(value: object) -> float | None:
        """Convert metric value to float when possible."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    @classmethod
    def _collect_latest_plan_metrics(cls, plan: dict | None) -> dict[str, float]:
        """Collect latest numeric metrics from completed experiments."""
        if not plan:
            return {}
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return {}

        metrics: dict[str, float] = {}
        for exp in experiments:
            if not isinstance(exp, dict) or exp.get("status") != "completed":
                continue
            results = exp.get("results")
            metric_map = results.get("metrics") if isinstance(results, dict) else None
            if not isinstance(metric_map, dict):
                continue
            for metric_name, raw_value in metric_map.items():
                if not isinstance(metric_name, str) or not metric_name.strip():
                    continue
                numeric = cls._to_number(raw_value)
                if numeric is None:
                    continue
                metrics[metric_name.strip()] = numeric
        return metrics

    @staticmethod
    def _count_completed_experiments(plan: dict | None) -> int:
        """Count completed experiments in task plan."""
        if not plan:
            return 0
        experiments = plan.get("experiments")
        if not isinstance(experiments, list):
            return 0
        return sum(1 for exp in experiments if isinstance(exp, dict) and exp.get("status") == "completed")

    @staticmethod
    def _compare_goal(metric_value: float, operator: str, target: float) -> bool:
        """Evaluate one metric threshold predicate."""
        if operator == ">":
            return metric_value > target
        if operator == ">=":
            return metric_value >= target
        if operator == "<":
            return metric_value < target
        if operator == "<=":
            return metric_value <= target
        if operator == "==":
            return abs(metric_value - target) <= 1e-9
        return False

    @classmethod
    def _evaluate_automation_stop_policy(
        cls,
        policy: dict[str, Any] | None,
        *,
        plan: dict | None,
        tokens_used: int,
    ) -> str | None:
        """Return stop reason if auto-stop policy threshold is reached."""
        if not policy:
            return None

        goals = policy.get("goals") if isinstance(policy.get("goals"), list) else []
        if goals:
            metrics = cls._collect_latest_plan_metrics(plan)
            evaluations: list[bool] = []
            for goal in goals:
                if not isinstance(goal, dict):
                    continue
                metric = goal.get("metric")
                operator = goal.get("operator")
                target = cls._to_number(goal.get("value"))
                if not isinstance(metric, str) or not metric.strip() or not isinstance(operator, str) or target is None:
                    continue
                metric_value = metrics.get(metric.strip())
                evaluations.append(
                    metric_value is not None and cls._compare_goal(metric_value, operator, target)
                )
            if evaluations:
                logic = str(policy.get("logic", "AND")).upper()
                goals_met = all(evaluations) if logic == "AND" else any(evaluations)
                if goals_met:
                    return "automation goals reached"

        max_experiments = policy.get("maxExperiments")
        if isinstance(max_experiments, int) and max_experiments > 0:
            completed = cls._count_completed_experiments(plan)
            if completed >= max_experiments:
                return f"max experiments reached ({completed}/{max_experiments})"

        max_tokens = policy.get("maxTokens")
        if isinstance(max_tokens, int) and max_tokens > 0 and tokens_used >= max_tokens:
            return f"token budget reached ({tokens_used}/{max_tokens})"

        return None
    @staticmethod
    def _plan_result_state(plan: dict | None) -> tuple[bool, Any]:
        """Return whether `result` exists and its payload."""
        if not isinstance(plan, dict):
            return False, None
        if "result" not in plan:
            return False, None
        return True, plan.get("result")

    @classmethod
    def _has_result_section_update(
        cls,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> bool:
        """Detect whether task_plan.result changed between rounds."""
        return cls._plan_result_state(before_plan) != cls._plan_result_state(after_plan)

    @staticmethod
    def _looks_like_result_request(content: object, metadata: dict[str, Any] | None = None) -> bool:
        """Detect explicit user intent to generate/export final deliverables."""
        if isinstance(metadata, dict) and bool(metadata.get("_allow_result_write")):
            return True
        if not isinstance(content, str):
            return False
        lowered = content.lower()
        if "manual export request for" in lowered:
            return True
        if "final deliverable" in lowered and "request" in lowered:
            return True
        if "导出" in content and ("报告" in content or "论文" in content or "结果" in content):
            return True
        return False

    def _restore_result_section(
        self,
        project_dir: str | None,
        *,
        before_plan: dict | None,
        after_plan: dict | None,
    ) -> tuple[dict | None, bool]:
        """Restore result section to previous state and persist task_plan."""
        if not project_dir or not isinstance(after_plan, dict):
            return after_plan, False
        if not self._has_result_section_update(before_plan, after_plan):
            return after_plan, False

        has_before_result, before_result = self._plan_result_state(before_plan)
        patched = json.loads(json.dumps(after_plan, ensure_ascii=False))
        if has_before_result:
            patched["result"] = before_result
        else:
            patched.pop("result", None)

        plan_path = Path(project_dir) / "task_plan.json"
        try:
            plan_path.write_text(
                json.dumps(patched, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return after_plan, False
        return patched, True
    def _guard_task_plan_structure(
        self,
        project_dir: str | None,
        *,
        auto_fix: bool = True,
        profile: str | None = None,
    ) -> bool:
        """Apply task_plan guardrails before auto-continue rounds."""
        if not project_dir:
            self._last_task_plan_guard_issues = []
            self._last_task_plan_guard_fixed = False
            return True
        result = guard_task_plan_file(Path(project_dir), auto_fix=auto_fix, profile=profile)
        issues = list(result.get("issues") or [])
        self._last_task_plan_guard_issues = issues
        self._last_task_plan_guard_fixed = bool(result.get("fixed"))
        if result.get("fixed"):
            logger.info("task_plan guardrails auto-fixed {}", project_dir)
        if result.get("blocking"):
            logger.warning(
                "task_plan guardrails blocked auto-continue for {}: {}",
                project_dir,
                issues[:3],
            )
            return False
        return True

    @staticmethod
    def _load_project_contract_version(project_dir: str | None) -> int:
        """Load project contract version from .medpilot/project.json."""
        if not project_dir:
            return 1
        meta_path = Path(project_dir) / ".medpilot" / "project.json"
        if not meta_path.is_file():
            return 1
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 1
        if isinstance(payload, dict):
            value = payload.get("contract_version")
            if isinstance(value, int) and value in {1, 2}:
                return value
        return 1

    def _build_task_plan_contract_hint(
        self,
        *,
        project_dir: str | None,
        agent_profile: str | None,
    ) -> str:
        """Build a concise contract hint for auto-run task_plan updates."""
        profile = self._parse_agent_profile(agent_profile) or "default"
        contract_version = self._load_project_contract_version(project_dir)
        contract = get_task_plan_contract(
            profile=profile,
            contract_version=contract_version,
        )
        required_completed = contract.get("required_completed_fields") or []
        required_falsify = contract.get("required_falsify_fields") or []

        lines = [
            "Task-plan contract requirements (enforce in this write):",
            f"- profile={profile}, contract_version={contract_version}",
        ]
        if required_completed:
            lines.append(
                "- when setting status=completed, include required fields: "
                + ", ".join(str(item) for item in required_completed)
            )
        if required_falsify:
            lines.append(
                "- when conclusion indicates rejection/failure, also include: "
                + ", ".join(str(item) for item in required_falsify)
            )
        lines.append(
            "- do not mark an experiment as completed unless required contract fields are present."
        )
        return "\n".join(lines)

    def _is_strict_contract_enforced(
        self, *, project_dir: str | None, agent_profile: str | None
    ) -> bool:
        """Whether current project is in strict contract mode with required fields."""
        profile = self._parse_agent_profile(agent_profile) or "default"
        contract_version = self._load_project_contract_version(project_dir)
        contract = get_task_plan_contract(
            profile=profile,
            contract_version=contract_version,
        )
        return (
            contract_version >= 2
            and bool(contract.get("required_completed_fields"))
        )

    def _build_auto_continue_message(
        self,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
        agent_profile: str | None = None,
    ) -> str:
        """Build the synthetic internal continue message for server-side auto mode."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        contract_hint = self._build_task_plan_contract_hint(
            project_dir=project_dir,
            agent_profile=agent_profile,
        )
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Auto-run checkpoint requirements:\n"
            "1) If you just finished an experiment, immediately update and write task_plan.json "
            "(set status/results/conclusion/next for that experiment) BEFORE starting the next one.\n"
            "2) Execute exactly ONE pending experiment in this round, then return control.\n"
            "3) Do not stop for confirmation unless user input is strictly required.\n\n"
            f"{contract_hint}"
        )

    def _build_auto_guardrail_repair_message(
        self,
        *,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
        issues: list[str],
    ) -> str:
        """Build an internal message asking model to patch blocked plan fields."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        issue_lines = "\n".join(f"- {item}" for item in issues[:8]) if issues else "- unknown issue"
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Guardrail validation blocked task_plan progression. "
            "Patch task_plan.json to satisfy the missing required fields only.\n"
            "Do NOT rewrite prior conclusions or metrics unless logically necessary.\n"
            "Use concrete evidence from experiment artifacts/results; "
            "placeholder text like 'Guardrail auto-fill: ...' is invalid in strict mode.\n"
            f"Missing/invalid items:\n{issue_lines}"
        )

    def _build_auto_checkpoint_sync_message(
        self,
        *,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
        running_ids: list[str],
        agent_profile: str | None = None,
    ) -> str:
        """Build an internal message that forces per-experiment task_plan checkpointing."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        contract_hint = self._build_task_plan_contract_hint(
            project_dir=project_dir,
            agent_profile=agent_profile,
        )
        items = "\n".join(f"- {item}" for item in running_ids[:8]) if running_ids else "- running experiment"
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Checkpoint barrier: task_plan.json still shows the same running experiment(s) as before this round.\n"
            "Before any new work, update and write task_plan.json now for the current running experiment(s):\n"
            "1) set final status (completed/failed/skipped) if finished;\n"
            "2) persist results/conclusion/next (or progress if still running);\n"
            "3) then stop this turn.\n"
            f"Running experiments to sync:\n{items}\n\n"
            f"{contract_hint}"
        )

    def _build_auto_guardrail_repair_message(
        self,
        *,
        channel: str,
        chat_id: str,
        project_dir: str | None,
        run_mode: str,
        issues: list[str],
    ) -> str:
        """Build an internal message asking model to patch blocked plan fields."""
        runtime_ctx = ContextBuilder._build_runtime_context(
            channel,
            chat_id,
            project_dir,
            run_mode=run_mode,
        )
        issue_lines = "\n".join(f"- {item}" for item in issues[:8]) if issues else "- unknown issue"
        return (
            f"{runtime_ctx}\n\n{self._AUTO_CONTINUE_MARKER}\n"
            "Guardrail validation blocked task_plan progression. "
            "Patch task_plan.json to satisfy the missing required fields only.\n"
            "Do NOT rewrite prior conclusions or metrics unless logically necessary.\n"
            "Use concrete evidence from experiment artifacts/results; "
            "placeholder text like 'Guardrail auto-fill: ...' is invalid in strict mode.\n"
            f"Missing/invalid items:\n{issue_lines}"
        )

    def _should_continue_auto_web(
        self,
        *,
        channel: str,
        run_mode: str,
        project_dir: str | None,
        final_content: str | None,
        auto_round: int,
        agent_profile: str | None = None,
    ) -> bool:
        """Decide whether to schedule another internal auto-run cycle."""
        if channel != "web" or run_mode != "auto":
            return False
        if not self._guard_task_plan_structure(project_dir, profile=agent_profile):
            return False
        if auto_round >= self._AUTO_MAX_ROUNDS:
            logger.warning("Auto mode max rounds ({}) reached", self._AUTO_MAX_ROUNDS)
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
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
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
            if control == "set_mode":
                await self._handle_set_mode(msg)
            elif msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            elif self._command_router.is_priority(msg.content):
                key = (
                    UNIFIED_SESSION_KEY
                    if self._unified_session and not msg.session_key_override
                    else msg.session_key
                )
                session = self.sessions.get_or_create(key)
                ctx = CommandContext(
                    msg=msg,
                    session=session,
                    key=key,
                    raw=msg.content.strip(),
                    loop=self,
                )
                response = await self._command_router.dispatch_priority(ctx)
                if response is not None:
                    await self.bus.publish_outbound(response)
            else:
                effective_key = (
                    UNIFIED_SESSION_KEY if self._unified_session and not msg.session_key_override else msg.session_key
                )
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(effective_key, []).append(task)
                task.add_done_callback(lambda t, k=effective_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

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
        if getattr(self, "_unified_session", False) and not msg.session_key_override:
            msg.session_key_override = UNIFIED_SESSION_KEY

        if bool((msg.metadata or {}).get("_wants_stream")):
            stream_meta = dict(msg.metadata or {})

            async def _on_stream(delta: str) -> None:
                if not delta:
                    return
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
                await self._process_message(msg, on_stream=_on_stream, on_stream_end=_on_stream_end)
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                err_text = "Sorry, I encountered an error."
                if msg.channel == "cli":
                    err_text += " Run `medpilot agent --logs` to view details."
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=err_text,
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
                    err_text += " Run `medpilot agent --logs` to view details."
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=err_text,
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

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
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
        agent_profile = self._resolve_session_agent_profile(key, meta.get("agent_profile"))
        automation_policy = self._resolve_session_automation_policy(
            key,
            meta.get("automation_policy"),
        )
        agents_filename = self._agent_profile_to_agents_filename(agent_profile)
        if project_dir:
            sessions_mgr = self._get_project_sessions(project_dir)
        else:
            sessions_mgr = self.sessions

        session = sessions_mgr.get_or_create(key)
        memory_workspace = Path(project_dir) if project_dir else self.workspace
        recent_skill_names = []
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
                self._session_model_runtimes.pop(session.key, None)
                if snapshot:
                    self._schedule_background(self.consolidator.archive(snapshot))
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="New session started.")
            ok = await self._consolidate_memory(session, archive_all=True, workspace_override=memory_workspace)
            if not ok:
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="Memory archival failed. Session was not reset.")
            session.clear()
            sessions_mgr.save(session)
            sessions_mgr.invalidate(session.key)
            self._session_model_runtimes.pop(session.key, None)
            self._session_automation_policies.pop(session.key, None)
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

        await self.consolidator.maybe_consolidate_by_tokens(session)
        history = session.get_history(max_messages=self.memory_window)
        model_runtime = self._get_model_runtime(key)
        extra_system = self._compose_extra_system(
            meta.get("_ui_system_instructions"),
            meta.get("_task_plan_guard_notice"),
        )

        ctx = ContextBuilder(memory_workspace) if project_dir else self.context
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
        current_turn_skills: set[str] = set()
        audit_cb = None
        emit_audit_to_channel = msg.channel == "web" or bool(meta.get("_emit_skill_audit"))
        allow_result_write = self._looks_like_result_request(msg.content, meta)
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
        round_plan_before = self._load_task_plan(project_dir) if msg.channel == "web" else None
        final_content, _, all_msgs = await self._run_agent_loop(initial_messages, **run_kwargs)
        total_tokens_used = self._last_loop_tokens_used
        round_plan_after = self._load_task_plan(project_dir) if msg.channel == "web" else None
        if msg.channel == "web" and not allow_result_write:
            round_plan_after, restored = self._restore_result_section(
                project_dir,
                before_plan=round_plan_before,
                after_plan=round_plan_after,
            )
            if restored:
                await progress_cb(
                    "auto-run guard: skipped task_plan.result update without explicit export request"
                )

        auto_round = 0
        guard_repair_round = 0
        checkpoint_repair_round = 0
        while True:
            current_mode = self._session_run_modes.get(key, run_mode)
            automation_policy = self._resolve_session_automation_policy(key, None)
            if msg.channel == "web" and current_mode == "auto":
                crossed = self._experiments_crossed_boundary(round_plan_before, round_plan_after)
                if len(crossed) > 1:
                    await progress_cb(
                        "auto-run guard warning: multiple experiments advanced in one round "
                        f"({', '.join(crossed[:3])})"
                    )
                if crossed and project_dir:
                    code_guard = self._guard_task_plan_structure(
                        project_dir,
                        auto_fix=True,
                        profile=agent_profile,
                    )
                    if code_guard and self._last_task_plan_guard_fixed:
                        await progress_cb(
                            "auto-run guard: code-level contract normalization applied "
                            "after experiment transition"
                        )
                        round_plan_after = self._load_task_plan(project_dir)
                if not self._has_experiment_checkpoint_update(round_plan_before, round_plan_after):
                    if checkpoint_repair_round >= self._AUTO_CHECKPOINT_REPAIR_MAX:
                        await progress_cb(
                            "auto-run guard warning: task_plan checkpoint missing after experiment round; "
                            "continuing due auto mode"
                        )
                    else:
                        checkpoint_repair_round += 1
                        auto_round += 1
                        running_ids = self._running_experiment_ids(round_plan_before)
                        await progress_cb(
                            f"auto-run checkpoint repair {checkpoint_repair_round}: "
                            "forcing task_plan sync for running experiment"
                        )
                        all_msgs.append({
                            "role": "user",
                            "content": self._build_auto_checkpoint_sync_message(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                project_dir=project_dir,
                                run_mode=current_mode,
                                running_ids=running_ids,
                                agent_profile=agent_profile,
                            ),
                        })
                        round_plan_before = round_plan_after
                        final_content, _, all_msgs = await self._run_agent_loop(
                            all_msgs,
                            model_runtime=model_runtime,
                            on_progress=progress_cb,
                            audit_hook=audit_cb,
                        )
                        total_tokens_used += self._last_loop_tokens_used
                        round_plan_after = self._load_task_plan(project_dir)
                        if msg.channel == "web" and not allow_result_write:
                            round_plan_after, restored = self._restore_result_section(
                                project_dir,
                                before_plan=round_plan_before,
                                after_plan=round_plan_after,
                            )
                            if restored:
                                await progress_cb(
                                    "auto-run guard: skipped task_plan.result update without explicit export request"
                                )
                        continue

            if msg.channel == "web" and current_mode == "auto":
                current_plan = self._load_task_plan(project_dir)
                stop_reason = self._evaluate_automation_stop_policy(
                    automation_policy,
                    plan=current_plan,
                    tokens_used=total_tokens_used,
                )
                if stop_reason:
                    await progress_cb(f"auto-run stop condition: {stop_reason}")
                    break

            should_continue = self._should_continue_auto_web(
                channel=msg.channel,
                run_mode=current_mode,
                project_dir=project_dir,
                final_content=final_content,
                auto_round=auto_round,
                agent_profile=agent_profile,
            )
            if not should_continue:
                guard_issues = list(getattr(self, "_last_task_plan_guard_issues", []))
                continue_despite_guard = False
                if (
                    msg.channel == "web"
                    and current_mode == "auto"
                    and guard_issues
                    and not self._looks_like_failure_response(final_content)
                    and not self._looks_like_user_input_request(final_content)
                ):
                    if guard_repair_round < self._AUTO_GUARD_REPAIR_MAX:
                        guard_repair_round += 1
                        auto_round += 1
                        await progress_cb(
                            f"auto-run guardrail repair {guard_repair_round}: "
                            "filling required evidence fields"
                        )
                        all_msgs.append({
                            "role": "user",
                            "content": self._build_auto_guardrail_repair_message(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                project_dir=project_dir,
                                run_mode=current_mode,
                                issues=guard_issues,
                            ),
                        })
                        guard_plan_before = round_plan_after if msg.channel == "web" else None
                        final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
                        total_tokens_used += self._last_loop_tokens_used
                        round_plan_after = self._load_task_plan(project_dir)
                        round_plan_before = guard_plan_before
                        if msg.channel == "web" and not allow_result_write:
                            round_plan_after, restored = self._restore_result_section(
                                project_dir,
                                before_plan=guard_plan_before,
                                after_plan=round_plan_after,
                            )
                            if restored:
                                await progress_cb(
                                    "auto-run guard: skipped task_plan.result update without explicit export request"
                                )
                        continue
                    has_pending = self._plan_has_pending_work(self._load_task_plan(project_dir))
                    strict_contract = self._is_strict_contract_enforced(
                        project_dir=project_dir,
                        agent_profile=agent_profile,
                    )
                    if strict_contract and has_pending and auto_round < self._AUTO_MAX_ROUNDS:
                        guard_repair_round = 0
                        auto_round += 1
                        await progress_cb(
                            "auto-run strict contract repair: required fields still missing; "
                            "requesting targeted completion before next experiment"
                        )
                        all_msgs.append({
                            "role": "user",
                            "content": self._build_auto_guardrail_repair_message(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                project_dir=project_dir,
                                run_mode=current_mode,
                                issues=guard_issues,
                            ),
                        })
                        guard_plan_before = round_plan_after if msg.channel == "web" else None
                        final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
                        total_tokens_used += self._last_loop_tokens_used
                        round_plan_after = self._load_task_plan(project_dir)
                        round_plan_before = guard_plan_before
                        if msg.channel == "web" and not allow_result_write:
                            round_plan_after, restored = self._restore_result_section(
                                project_dir,
                                before_plan=guard_plan_before,
                                after_plan=round_plan_after,
                            )
                            if restored:
                                await progress_cb(
                                    "auto-run guard: skipped task_plan.result update without explicit export request"
                                )
                        continue
                    if has_pending and auto_round < self._AUTO_MAX_ROUNDS:
                        continue_despite_guard = True
                        await progress_cb(
                            "auto-run guard warning: contract issues remain after repair; "
                            "continuing and deferring strict cleanup"
                        )
                if not continue_despite_guard:
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
                    agent_profile,
                ),
            })
            round_plan_before = round_plan_after
            final_content, _, all_msgs = await self._run_agent_loop(all_msgs, **run_kwargs)
            total_tokens_used += self._last_loop_tokens_used
            round_plan_after = self._load_task_plan(project_dir)
            if msg.channel == "web" and not allow_result_write:
                round_plan_after, restored = self._restore_result_section(
                    project_dir,
                    before_plan=round_plan_before,
                    after_plan=round_plan_after,
                )
                if restored:
                    await progress_cb(
                        "auto-run guard: skipped task_plan.result update without explicit export request"
                    )

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
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            meta = c.get("_meta")
                            path = meta.get("path") if isinstance(meta, dict) else None
                            filtered.append({"type": "text", "text": f"[image: {path}]" if path else "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

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
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> OutboundMessage | str | None:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
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
