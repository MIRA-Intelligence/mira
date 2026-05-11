"""Subagent manager for background task execution."""

import asyncio
import uuid
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from mira_engine.agent.routing import ModelRouter, RoutedProviderManager
from mira_engine.agent.runner import AgentRunner, AgentRunSpec
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
from mira_engine.bus.events import InboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ExecToolConfig
from mira_engine.providers.base import LLMProvider


class SubagentManager:
    """Manages background subagent execution."""

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
        model_router: ModelRouter | None = None,
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
        self.model_router = model_router
        self.max_tool_result_chars = max_tool_result_chars
        self.runner = AgentRunner(provider)
        self._session_runtimes: dict[str, RoutedProviderManager] = {}
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    def _get_runtime(self, session_key: str) -> RoutedProviderManager:
        """Return the session-local routed provider runtime for subagents."""
        runtime = self._session_runtimes.get(session_key)
        if runtime is None:
            runtime = RoutedProviderManager(
                default_provider=self.provider,
                default_model=self.model,
                router=self.model_router,
                provider_factory=self.provider_factory,
            )
            self._session_runtimes[session_key] = runtime
        return runtime

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        workspace: Path | None = None,
        project_id: str | None = None,
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
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        run_workspace = workspace or self.workspace

        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            allowed_dir = run_workspace if self.restrict_to_workspace else None
            tools.register(ReadFileTool(workspace=run_workspace, allowed_dir=allowed_dir))
            tools.register(WriteFileTool(workspace=run_workspace, allowed_dir=allowed_dir))
            tools.register(EditFileTool(workspace=run_workspace, allowed_dir=allowed_dir))
            tools.register(ListDirTool(workspace=run_workspace, allowed_dir=allowed_dir))
            tools.register(GrepTool(workspace=run_workspace, allowed_dir=allowed_dir))
            tools.register(GlobTool(workspace=run_workspace, allowed_dir=allowed_dir))
            if self.exec_config.enable:
                tools.register(ExecTool(
                    working_dir=str(run_workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    path_append=self.exec_config.path_append,
                    python_runtime=self.exec_config.python,
                ))
            tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
            tools.register(WebFetchTool(proxy=self.web_proxy))

            system_prompt = self._build_subagent_prompt(run_workspace)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            result = await self.runner.run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    max_iterations=15,
                    max_iterations_message="Task completed but no final response was generated.",
                    max_tool_result_chars=self.max_tool_result_chars,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    reasoning_effort=self.reasoning_effort,
                    fail_on_tool_error=False,
                )
            )
            final_result = result.final_content or "Task completed but no final response was generated."
            status = "ok"
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
        status_text = "completed successfully" if status == "ok" else "failed"

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

    def _build_subagent_prompt(self, workspace: Path | None = None) -> str:
        """Build a focused system prompt for the subagent."""
        from mira_engine.agent.context import ContextBuilder

        run_workspace = workspace or self.workspace
        context = ContextBuilder(run_workspace)
        time_ctx = ContextBuilder._build_runtime_context(None, None)
        parts = [context.build_system_prompt(), f"""# Subagent

{time_ctx}

You are a subagent spawned by the main agent to complete a specific task.
Stay focused on the assigned task. Your final response will be reported back to the main agent.

## Workspace
{run_workspace}"""]

        return "\n\n".join(parts)

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
