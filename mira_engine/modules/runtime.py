"""Runtime adapter routing ModuleRuns around the legacy ResearchAgentLoop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService
from mira_engine.workflows.confirmations import (
    confirmation_approved,
    confirmation_required,
    expected_confirmation_type,
)
from mira_engine.workflows.models import new_id

from .base import ModuleExecutionContext, ModuleInvocation
from .executors import ModuleExecutorRegistry
from .profiles import normalize_agent_profile

if TYPE_CHECKING:
    from mira_engine.agent.research_loop import ResearchAgentLoop


class NativeModuleRuntime:
    """Execute one modular turn through a clean BaseAgentLoop path."""

    def __init__(self, registry: ModuleExecutorRegistry | None = None) -> None:
        self.registry = registry or ModuleExecutorRegistry()

    async def process(
        self,
        loop: ResearchAgentLoop,
        msg: InboundMessage,
        *,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        audit_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        metadata = dict(msg.metadata or {})
        run_id = metadata.get("workflow_run_id")
        project_ref = loop._project_ref_from_metadata(metadata)
        if not isinstance(run_id, str) or not run_id or project_ref is None:
            raise ValueError("native module execution requires Project and workflow_run_id")

        repository = JsonWorkflowRepository(project_ref.project_dir, project_ref.project_id)
        service = WorkflowService(repository)
        run = repository.load_run(run_id)
        active_attempt = next(
            (
                candidate
                for candidate in reversed(run.attempts)
                if candidate.attempt_id == run.attempt.current_attempt_id
            ),
            None,
        )
        project_document = repository.load_project_document()
        agent_profile = normalize_agent_profile(
            active_attempt.agent_profile
            if active_attempt is not None
            else metadata.get("agent_profile", project_document.get("agent_profile"))
        )
        artifacts = tuple(repository.load_artifact(selected.artifact_id) for selected in run.inputs)
        executor = self.registry.get(run.module_type)
        context = ModuleExecutionContext(
            run=run,
            artifacts=artifacts,
            project_dir=project_ref.project_dir,
            user_content=msg.content,
            metadata=metadata,
            agent_profile=agent_profile,
        )

        async def invoke(invocation: ModuleInvocation):
            clean_metadata = {
                key: value
                for key, value in metadata.items()
                if key
                not in {
                    "_plan_event",
                    "_plan_decision",
                    "_plan_feedback",
                    "_task_plan_guard_notice",
                    "automation_policy",
                }
            }
            clean_metadata["run_mode"] = "manual"
            clean_metadata["_ui_system_instructions"] = invocation.system_instructions
            clean_metadata["_audit_context"] = {
                "project_id": project_ref.project_id,
                "run_id": run_id,
                "module_type": run.module_type,
                "executor_name": executor.descriptor.executor_name,
                "executor_version": executor.descriptor.executor_version,
                "agent_profile": agent_profile,
            }
            prepared = InboundMessage(
                channel=msg.channel,
                sender_id=msg.sender_id,
                chat_id=msg.chat_id,
                content=msg.content,
                timestamp=msg.timestamp,
                media=list(msg.media),
                metadata=clean_metadata,
                session_key_override=msg.session_key_override,
            )
            visible_tools = set(loop.tools.all_tool_names) - set(invocation.blocked_tools)
            pre_confirmation_tools = {
                "read_file",
                "list_dir",
                "glob",
                "grep",
                "request_workflow_interaction",
            }

            async def control_guard(_tool_name: str, _arguments: dict[str, Any]) -> str | None:
                current = repository.load_run(run_id)
                if current.status == "running" and current.pending_control in {"pause", "cancel"}:
                    control = current.pending_control
                    service.acknowledge_control(
                        run_id,
                        command_id=f"executor-control:{new_id()}",
                        control=control,
                        attempt_id=current.attempt.current_attempt_id,
                        executor_name=executor.descriptor.executor_name,
                        executor_version=executor.descriptor.executor_version,
                        checkpoint_state={
                            "before_tool_call": True,
                            "executor_name": executor.descriptor.executor_name,
                        },
                    )
                    return (
                        '{"workflow_control":"acknowledged","control":"'
                        + control
                        + '","instruction":"Stop this turn without more tool calls."}'
                    )
                if current.status in {"paused", "completed", "failed", "cancelled"}:
                    return (
                        '{"workflow_control":"already_closed","status":"'
                        + current.status
                        + '","instruction":"Stop this turn without more tool calls."}'
                    )
                if (
                    confirmation_required(current)
                    and not confirmation_approved(current)
                    and _tool_name not in pre_confirmation_tools
                ):
                    interaction_type = expected_confirmation_type(current.module_type)
                    return (
                        '{"workflow_confirmation":"required","interaction_type":"'
                        + interaction_type
                        + '","blocked_tool":"'
                        + _tool_name
                        + '","instruction":"Draft the module execution brief and call '
                        "request_workflow_interaction before consequential work." + '"}'
                    )
                return None

            async def completion_guard(_content: str | None) -> str | None:
                current = repository.load_run(run_id)
                if current.status != "running" or current.pending_control is not None:
                    return None
                if confirmation_required(current) and not confirmation_approved(current):
                    interaction_type = expected_confirmation_type(current.module_type)
                    return (
                        "This ModuleRun still requires approval of its execution brief. "
                        f"Draft or revise the brief and call request_workflow_interaction "
                        f"with interaction_type `{interaction_type}`, then end the turn."
                    )
                literature = current.module_data.get("literature_review")
                research_missing = (
                    current.module_type == "initiation"
                    and isinstance(literature, dict)
                    and literature.get("enabled") is True
                    and not isinstance(current.module_data.get("research_result"), dict)
                )
                if research_missing:
                    return (
                        "The Initiation Run is still running and has no structured research "
                        "result. A progress statement is not a stopping point. Continue now: "
                        "execute the selected-source searches (call pubmed_search when PubMed "
                        "is selected), inspect the evidence, record the structured result, "
                        "then request a direction choice or complete the Run. Do not emit "
                        "another standalone promise to search."
                    )
                return None

            with loop.tools.restrict_to(visible_tools, execution_guard=control_guard):
                return await BaseAgentLoop._process_message(
                    loop,
                    prepared,
                    session_key=session_key,
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                    audit_hook=audit_hook,
                    completion_guard=completion_guard,
                )

        try:
            response = await executor.execute(context, invoke)
        except ValueError as exc:
            current = repository.load_run(run_id)
            if current.status in {"queued", "running", "waiting_input"}:
                service.command_run(
                    run_id,
                    command_id=f"executor-validation:{new_id()}",
                    command_type="fail",
                    expected_revision=current.revision,
                    payload={
                        "code": "input_invalid",
                        "message": str(exc),
                        "retryable": True,
                    },
                )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"Module input validation failed: {exc}",
                metadata={**metadata, "_error": True, "error_code": "input_invalid"},
            )

        current = repository.load_run(run_id)
        if current.status == "running" and current.pending_control in {"pause", "cancel"}:
            control = current.pending_control
            service.acknowledge_control(
                run_id,
                command_id=f"executor-control:{new_id()}",
                control=control,
                attempt_id=current.attempt.current_attempt_id,
                executor_name=executor.descriptor.executor_name,
                executor_version=executor.descriptor.executor_version,
                checkpoint_state={
                    "conversation_boundary": True,
                    "executor_name": executor.descriptor.executor_name,
                },
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "Module paused at a safe conversation boundary."
                    if control == "pause"
                    else "Module cancelled at a safe conversation boundary."
                ),
                metadata={**metadata, "_workflow_control_ack": control},
            )
        if current.status in {"paused", "cancelled"} and (
            current.last_transition
            and current.last_transition.event_type
            in {"run.pause_acknowledged", "run.cancel_acknowledged"}
        ):
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "Module paused at a safe tool boundary."
                    if current.status == "paused"
                    else "Module cancelled at a safe tool boundary."
                ),
                metadata={**metadata, "_workflow_control_ack": current.status},
            )
        literature = current.module_data.get("literature_review")
        incomplete_enabled_research = (
            current.status == "running"
            and current.module_type == "initiation"
            and isinstance(literature, dict)
            and literature.get("enabled") is True
            and not isinstance(current.module_data.get("research_result"), dict)
        )
        if incomplete_enabled_research:
            service.command_run(
                run_id,
                command_id=f"executor-incomplete:{new_id()}",
                command_type="fail",
                expected_revision=current.revision,
                payload={
                    "code": "executor_incomplete",
                    "message": (
                        "The native executor ended without recording a workflow "
                        "interaction or completion."
                    ),
                    "retryable": True,
                },
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "Module execution stopped before producing a durable result. "
                    "Retry the Run to continue."
                ),
                metadata={**metadata, "_error": True, "error_code": "executor_incomplete"},
            )
        return response
