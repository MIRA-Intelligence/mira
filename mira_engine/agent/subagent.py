"""Subagent manager for background task execution."""

import asyncio
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from mira_engine.agent.hook import AgentHook, CompositeHook
from mira_engine.agent.routing import RoutedProviderManager
from mira_engine.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from mira_engine.agent.skill_events import skill_loaded, skill_read_details
from mira_engine.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.agent.tools.search import GlobTool, GrepTool
from mira_engine.agent.tools.shell import ExecTool
from mira_engine.agent.tools.web import WebFetchTool, WebSearchTool
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ExecToolConfig
from mira_engine.providers.base import LLMProvider

# ``team`` profile role definitions. Each role gets its own focused prompt,
# sampling settings, and tool allowlist. ``allowed_tools = None`` means the
# full subagent toolset; an explicit set restricts to those tool names. The
# concrete provider/model for each role is resolved at runtime via the
# loop's ``role_provider_factory`` (see SubagentManager._resolve_role).
ROLE_CONFIGS: dict[str, dict[str, Any]] = {
    "student": {
        # Implementer: full read/write/exec/search/web toolset, no spawning.
        "allowed_tools": None,
        "temperature": 0.1,
    },
    "critic": {
        # Reviewer: read-only. No write/edit/exec — it only inspects and
        # returns a verdict, so it must not mutate the workspace.
        "allowed_tools": {"read_file", "list_dir", "grep", "glob", "web_search", "web_fetch"},
        "temperature": 0.1,
    },
    "supervisor": {
        # Planner/orchestrator. Rarely run as a subagent (the main loop plays
        # supervisor); included for completeness / direct consults.
        "allowed_tools": None,
        "temperature": 0.2,
    },
}


@dataclass(frozen=True)
class DelegationPolicy:
    """Attempt-local authority inherited by synchronous and background delegates."""

    scope_id: str
    allowed_tools: frozenset[str]
    system_instructions: str
    execution_guard: Callable[[str, dict[str, Any]], Awaitable[str | None]]
    stop_guard: Callable[[], Awaitable[str | None]]
    run_id: str
    read_paths: tuple[Path, ...] = ()


class _DelegationStoppedError(Exception):
    pass


class _DelegationHook(AgentHook):
    def __init__(self, policy: DelegationPolicy):
        super().__init__(reraise=True)
        self.policy = policy

    async def before_iteration(self, context):
        reason = await self.policy.stop_guard()
        if reason is not None:
            raise _DelegationStoppedError(reason)


class _SkillAuditHook(AgentHook):
    def __init__(self, callback, agent: str):
        super().__init__()
        self.callback = callback
        self.agent = agent

    async def after_iteration(self, context):
        delivered = {message.get("tool_call_id"): message.get("content")
                     for message in context.messages if message.get("role") == "tool"}
        for call, result in zip(context.tool_calls, context.tool_results):
            if call.id not in delivered:
                continue
            event = skill_read_details(call.name, call.arguments, result)
            if event:
                event["partial"] = event["partial"] or delivered[call.id] != result
                await self.callback({**event, "agent": self.agent})


class SubagentManager:
    """Manages background subagent execution and synchronous role consults."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        provider_factory: Callable[[str], LLMProvider] | None = None,
        model_candidates: list[str] | None = None,
        role_provider_factory: "Callable[[str], tuple[LLMProvider, str, tuple[str, ...]]] | None" = None,
        max_tool_result_chars: int = 16_000,
    ):
        from mira_engine.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.provider_factory = provider_factory
        self.model_candidates = list(model_candidates) if model_candidates else None
        self.role_provider_factory = role_provider_factory
        self.max_tool_result_chars = max_tool_result_chars
        self.runner = AgentRunner(provider)
        self._session_runtimes: dict[str, RoutedProviderManager] = {}
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        # Synchronous consult sessions: consult_id -> accumulated messages.
        # Lets the supervisor hold a multi-turn debate with the same critic.
        self._consult_sessions: dict[str, list[dict[str, Any]]] = {}
        self._consult_scopes: dict[str, tuple[str, str, str | None]] = {}
        self._delegation_policy: ContextVar[DelegationPolicy | None] = ContextVar(
            f"delegation_policy_{id(self)}", default=None
        )
        self._skill_audit: ContextVar[Any] = ContextVar(f"skill_audit_{id(self)}", default=None)

    @contextmanager
    def skill_audit_scope(self, callback):
        token = self._skill_audit.set(callback)
        try:
            yield
        finally:
            self._skill_audit.reset(token)

    async def _skill_hook(self, agent: str, loaded_skills: list[str]):
        callback = self._skill_audit.get()
        if callback is None:
            return None
        for name in loaded_skills:
            await callback(skill_loaded(name, "always", agent=agent))
        return _SkillAuditHook(callback, agent)

    @contextmanager
    def delegation_scope(self, policy: DelegationPolicy):
        token = self._delegation_policy.set(policy)
        try:
            yield
        finally:
            self._delegation_policy.reset(token)

    async def _run_delegate(self, runner: AgentRunner, spec: AgentRunSpec) -> AgentRunResult:
        policy = self._delegation_policy.get()
        if policy is None:
            return await runner.run(spec)
        spec.hook = CompositeHook([_DelegationHook(policy), spec.hook] if spec.hook else [_DelegationHook(policy)])
        try:
            with spec.tools.restrict_to(policy.allowed_tools, execution_guard=policy.execution_guard):
                return await runner.run(spec)
        except _DelegationStoppedError as exc:
            return AgentRunResult(final_content=str(exc), messages=spec.initial_messages,
                                  stop_reason="parent_closed")

    def _resolve_role(self, role: str | None) -> tuple[LLMProvider, str, AgentRunner]:
        """Return (provider, model, runner) for a role, falling back to defaults."""
        if role and self.role_provider_factory is not None:
            try:
                provider, model, _candidates = self.role_provider_factory(role)
                return provider, model, AgentRunner(provider)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Role provider resolution failed for '{}': {}", role, exc)
        return self.provider, self.model, self.runner

    def _get_runtime(self, session_key: str) -> RoutedProviderManager:
        """Return the session-local routed provider runtime for subagents."""
        runtime = self._session_runtimes.get(session_key)
        if runtime is None:
            runtime = RoutedProviderManager(
                default_provider=self.provider,
                default_model=self.model,
                provider_factory=self.provider_factory,
                default_candidates=tuple(self.model_candidates or [self.model]),
            )
            self._session_runtimes[session_key] = runtime
        return runtime

    def _build_tools(self, run_workspace: Path, role: str | None = None) -> ToolRegistry:
        """Build the subagent toolset, restricted by role allowlist when set."""
        allowed = ROLE_CONFIGS.get(role or "", {}).get("allowed_tools")
        policy = self._delegation_policy.get()

        def _permitted(name: str) -> bool:
            return (allowed is None or name in allowed) and (
                policy is None or name in policy.allowed_tools
            )

        tools = ToolRegistry()
        allowed_dir = run_workspace if self.restrict_to_workspace else None
        read_paths = policy.read_paths if policy else ()
        supports_vision = self.provider.supports_vision(self.model)
        if _permitted("read_file"):
            tools.register(ReadFileTool(workspace=run_workspace, allowed_dir=allowed_dir, supports_vision=supports_vision, read_paths=read_paths))
        if _permitted("write_file"):
            tools.register(WriteFileTool(workspace=run_workspace, allowed_dir=allowed_dir))
        if _permitted("edit_file"):
            tools.register(EditFileTool(workspace=run_workspace, allowed_dir=allowed_dir))
        if _permitted("list_dir"):
            tools.register(ListDirTool(workspace=run_workspace, allowed_dir=allowed_dir, read_paths=read_paths))
        if _permitted("grep"):
            tools.register(GrepTool(workspace=run_workspace, allowed_dir=allowed_dir, read_paths=read_paths))
        if _permitted("glob"):
            tools.register(GlobTool(workspace=run_workspace, allowed_dir=allowed_dir, read_paths=read_paths))
        if self.exec_config.enable and _permitted("exec"):
            tools.register(ExecTool(
                working_dir=str(run_workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
                python_runtime=self.exec_config.python,
            ))
        if _permitted("web_search"):
            tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        if _permitted("web_fetch"):
            tools.register(WebFetchTool(proxy=self.web_proxy))
        return tools

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        workspace: Path | None = None,
        project_id: str | None = None,
        role: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id}
        run_workspace = workspace or self.workspace

        runtime_key = session_key or f"subagent:{task_id}"
        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                self._get_runtime(runtime_key),
                workspace=run_workspace,
                project_id=project_id,
                role=role,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        provider_runtime: RoutedProviderManager | None = None,
        workspace: Path | None = None,
        project_id: str | None = None,
        role: str | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        run_workspace = workspace or self.workspace

        try:
            tools = self._build_tools(run_workspace, role)
            provider, model, runner = self._resolve_role(role)
            role_temperature = ROLE_CONFIGS.get(role or "", {}).get("temperature", self.temperature)

            loaded_skills: list[str] = []
            system_prompt = self._build_subagent_prompt(run_workspace, role, loaded_skills)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            result = await self._run_delegate(runner,
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=model,
                    max_iterations=15,
                    max_iterations_message="Task completed but no final response was generated.",
                    max_tool_result_chars=self.max_tool_result_chars,
                    temperature=role_temperature,
                    max_tokens=self.max_tokens,
                    reasoning_effort=self.reasoning_effort,
                    fail_on_tool_error=False,
                    hook=await self._skill_hook(f"{role or 'subagent'}:{task_id}", loaded_skills),
                )
            )
            final_result = result.final_content or "Task completed but no final response was generated."
            status = "ok" if result.stop_reason != "parent_closed" else "stopped"
            if any(e.get("status") == "error" for e in result.tool_events):
                completed = [e for e in result.tool_events if e.get("status") == "ok"]
                errors = [e for e in result.tool_events if e.get("status") == "error"]
                lines = []
                if completed:
                    lines.append("Completed steps:")
                    for e in completed:
                        lines.append(f"- {e.get('name')}: {e.get('detail')}")
                if errors:
                    lines.append("Failure:")
                    for e in errors:
                        detail = str(e.get("detail") or "")
                        if detail.startswith("Error executing ") and ": " in detail:
                            detail = detail.split(": ", 1)[1]
                        if " [Analyze the error above and try a different approach.]" in detail:
                            detail = detail.split(" [Analyze the error above and try a different approach.]", 1)[0]
                        lines.append(f"- {e.get('name')}: {detail}")
                final_result = "\n".join(lines) if lines else final_result
                status = "error"

            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(
                task_id,
                label,
                task,
                final_result,
                origin,
                status,
                run_workspace,
                project_id,
            )

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(
                task_id,
                label,
                task,
                error_msg,
                origin,
                "error",
                run_workspace,
                project_id,
            )

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        workspace: Path | None = None,
        project_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = {"ok": "completed successfully", "stopped": "stopped with its parent Run"}.get(status, "failed")

        policy = self._delegation_policy.get()
        if policy is not None:
            # A native delegate must not trigger an unscoped legacy system turn.
            # Notify the owning Run only; its supervisor owns further execution.
            await self.bus.publish_outbound(OutboundMessage(
                channel=origin["channel"], chat_id=origin["chat_id"],
                content=f"[Subagent '{label}' {status_text}]\n{result}",
                metadata={"project_id": project_id, "workflow_run_id": policy.run_id,
                          "_progress": True, "_subagent_result": True},
            ))
            return

        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            metadata={
                key: value
                for key, value in {
                    "project_id": project_id,
                    "project_dir": str(workspace) if workspace else None,
                }.items()
                if value
            },
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    # Role-specific instruction blocks appended to the base subagent prompt.
    _ROLE_PROMPTS: dict[str, str] = {
        "student": """## Role: Student (Implementer)
You are the implementer on a research team. The supervisor has handed you a
concrete, already-approved task. Execute it directly and thoroughly:
- Do the actual work (write/edit files, run code) — do NOT re-plan or re-debate scope.
- Keep a tight todo discipline and verify your changes before reporting.
- You CANNOT delegate or spawn other agents; you work alone on implementation.
- Report what you did, what you verified, and any blockers.""",
        "critic": """## Role: Critic (Reviewer / Red Team)
You are the skeptical reviewer on a research team. You evaluate the supervisor's
plan for scientific rigor, falsifiability, confounds, and feasibility. You are
READ-ONLY: you inspect but never modify the workspace.

Boundaries:
- Challenge rigor, hidden assumptions, missing controls/ablations, and whether
  claims are falsifiable — NOT cosmetic preferences.
- Respect the chosen research direction; critique whether it is sound and
  well-specified enough to execute, not whether a different topic would be "better".

End EVERY review with a machine-readable verdict line, exactly:
`**[OKAY]**` if the plan is rigorous and ready, or `**[REJECT]**` if not,
followed by the top 3-5 concrete improvements needed.""",
        "supervisor": """## Role: Supervisor (Planner)
You design the research strategy and decompose it into precise, executable tasks.
Specify hypotheses, controls, and acceptance criteria so an implementer can act
without guessing.""",
    }

    def _build_subagent_prompt(
        self, workspace: Path | None = None, role: str | None = None,
        loaded_skills: list[str] | None = None,
    ) -> str:
        """Build a focused system prompt for the subagent (role-aware)."""
        from mira_engine.agent.context import ContextBuilder

        run_workspace = workspace or self.workspace
        context = ContextBuilder(run_workspace)
        time_ctx = ContextBuilder._build_runtime_context(None, None)
        parts = [context.build_system_prompt(loaded_skills=loaded_skills), f"""# Subagent

{time_ctx}

You are a subagent spawned by the main agent to complete a specific task.
Stay focused on the assigned task. Your final response will be reported back to the main agent.

## Workspace
{run_workspace}"""]
        role_block = self._ROLE_PROMPTS.get(role or "")
        if role_block:
            parts.append(role_block)
        policy = self._delegation_policy.get()
        if policy is not None:
            parts.append(
                "# Inherited ModuleRun contract\n" + policy.system_instructions
                + "\nYou are a delegate, not the supervisor. Perform only the assigned subset. "
                  "Do not start other stages or delegate further. Return findings to the supervisor; "
                  "only it may request user approval, publish artifacts, or complete the Run."
            )

        return "\n\n".join(parts)

    async def consult(
        self,
        role: str,
        task: str,
        workspace: Path | None = None,
        session_id: str | None = None,
        max_iterations: int = 15,
    ) -> tuple[str, str]:
        """Run a role subagent synchronously and return (result, session_id).

        Used by the supervisor (main loop) to get a critic verdict or a student
        result inline. Passing back the returned ``session_id`` continues the
        same conversation, enabling a multi-turn debate with the same role.
        """
        run_workspace = workspace or self.workspace
        provider, model, runner = self._resolve_role(role)
        role_temperature = ROLE_CONFIGS.get(role, {}).get("temperature", self.temperature)
        tools = self._build_tools(run_workspace, role)

        consult_id = session_id or f"consult:{role}:{uuid.uuid4().hex[:8]}"
        policy = self._delegation_policy.get()
        scope = (str(run_workspace.resolve()), role, policy.scope_id if policy else None)
        if session_id and self._consult_scopes.get(session_id) != scope:
            raise ValueError("consult session does not belong to this project, role, and Run attempt")
        self._consult_scopes[consult_id] = scope
        history = self._consult_sessions.get(consult_id)
        loaded_skills: list[str] = []
        if history is None:
            history = [{"role": "system", "content": self._build_subagent_prompt(run_workspace, role, loaded_skills)}]
        messages = list(history)
        messages.append({"role": "user", "content": task})

        result = await self._run_delegate(runner,
            AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=model,
                max_iterations=max_iterations,
                max_iterations_message="Consult finished without a final response.",
                max_tool_result_chars=self.max_tool_result_chars,
                temperature=role_temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
                fail_on_tool_error=False,
                hook=await self._skill_hook(consult_id, loaded_skills),
            )
        )
        # Persist the accumulated conversation so a follow-up consult with the
        # same session_id continues the debate instead of restarting it.
        self._consult_sessions[consult_id] = result.messages
        final = result.final_content or "Consult finished without a final response."
        return final, consult_id

    def clear_consult_session(self, session_id: str) -> None:
        """Drop a stored consult conversation."""
        self._consult_sessions.pop(session_id, None)
        self._consult_scopes.pop(session_id, None)

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
