"""Runtime adapter routing ModuleRuns around the legacy ResearchAgentLoop."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.subagent import DelegationPolicy
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService
from mira_engine.workflows.confirmations import (
    confirmation_approved,
    confirmation_required,
    expected_confirmation_type,
)
from mira_engine.workflows.experiments import describe_replan, experiment_boundary_decision, manual_experiments
from mira_engine.workflows.input_paths import run_read_paths
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
            clean_metadata["agent_profile"] = agent_profile
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
                # The service permits only pending records before scope approval.
                "record_workflow_experiment",
            }

            def request_manual_boundary(current):
                boundary = current.module_data.get("_manual_experiment_boundary")
                finished = next((
                    item for item in current.module_data.get("experiments") or []
                    if isinstance(boundary, dict) and item.get("id") == boundary.get("experiment_id")
                ), None)
                # Wait for the replan decision so the user confirms a concrete proposal.
                if (
                    current.status == "running" and current.pending_control is None
                    and manual_experiments(current) and boundary
                    and experiment_boundary_decision(current) is None
                    and (finished is None or finished.get("replan") or finished.get("status") == "skipped")
                ):
                    proposal = (
                        f"\n建议下一步 / Proposed next step: {describe_replan(finished)}"
                        if finished and finished.get("replan") else ""
                    )
                    service.request_interaction(
                        run_id, command_id=f"experiment-boundary:{boundary['interaction_id']}",
                        attempt_id=current.attempt.current_attempt_id,
                        interaction={
                            "interaction_id": boundary["interaction_id"],
                            "interaction_type": "confirm_experiment_continuation",
                            "title": f"实验 {boundary['experiment_id']} 已结束：确认下一步",
                            "prompt": f"{boundary['title']}{proposal}\n手动模式已保存本实验记录。按建议继续执行下一个实验，还是结束并汇总？\nManual mode: the experiment record is saved. Continue with one more experiment as proposed or finish and summarize?",
                            "choices": [
                                {"value": "continue", "label": "按建议继续 / Continue as proposed"},
                                {"value": "finish", "label": "结束并汇总 / Finish and summarize"},
                            ],
                            "response_schema": {"type": "object", "required": ["selection"], "properties": {
                                "selection": {"type": "string", "enum": ["continue", "finish"]},
                                "notes": {"type": "string"},
                            }, "additionalProperties": False},
                            "allow_freeform": True,
                        },
                    )
                    return repository.load_run(run_id)
                return current

            async def control_guard(_tool_name: str, _arguments: dict[str, Any]) -> str | None:
                current = request_manual_boundary(repository.load_run(run_id))
                if (
                    current.status != "running"
                    or current.attempt.current_attempt_id != run.attempt.current_attempt_id
                ):
                    return (
                        '{"workflow_control":"turn_closed","status":"'
                        + current.status
                        + '","instruction":"Stop. This attempt has ended; wait for the user response or a new attempt."}'
                    )
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
                if manual_experiments(current) and _tool_name not in {
                    "read_file", "list_dir", "glob", "grep", "write_file", "edit_file",
                    "request_workflow_interaction", "record_workflow_experiment",
                    "publish_workflow_artifact", "complete_workflow_run",
                } and not any(item.get("status") == "running" for item in current.module_data.get("experiments", [])):
                    return (
                        "Manual experiment mode: no experiment is active. Register exactly one "
                        "experiment as running with record_workflow_experiment before execution. "
                        "If the user chose finish, skip pending experiments and publish the summary instead."
                    )
                return None

            async def turn_stop_guard() -> str | None:
                current = request_manual_boundary(repository.load_run(run_id))
                if current.status == "waiting_input":
                    interaction = current.module_data.get("interaction") or {}
                    title = str(interaction.get("title", ""))
                    if any("\u3400" <= char <= "\u9fff" for char in title):
                        return f"等待你在交互表单中确认：{title}"
                    return f"Waiting for your response in the interaction form: {title}"
                if current.status == "completed":
                    return current.module_data.get("completion_summary") or "Module completed."
                if current.status != "running" or (
                    current.attempt.current_attempt_id != run.attempt.current_attempt_id
                ):
                    return f"This module turn has ended (status: {current.status})."
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
                if current.module_type == "experiment" and current.module_data.get("experiments"):
                    if experiment_boundary_decision(current) == "finish":
                        return (
                            "The user chose finish. Do not execute more experiments. Mark remaining "
                            "pending proposals skipped, publish existing results, and complete_workflow_run."
                        )
                    unplanned = [
                        item["id"] for item in current.module_data["experiments"]
                        if item.get("status") in {"completed", "failed"} and not item.get("replan")
                    ]
                    if unplanned:
                        return (
                            f"Experiment(s) {', '.join(unplanned)} finished without a replan decision. Reflect on "
                            "the outcome and update the same id with replan (branch/revise/skip/continue/finish, "
                            "rationale, children/revised/skipped/next) before continuing or completing."
                        )
                    return (
                        "This Experiment Run is still running. A progress statement is not completion. "
                        "Continue the approved registered experiments within the configured limits; "
                        "record their outcomes. In manual mode the runtime will stop at each experiment "
                        "boundary. If scope is finished, skip remaining proposals as appropriate, publish "
                        "results and call complete_workflow_run. If blocked, request_workflow_interaction."
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

            async def delegation_stop_guard() -> str | None:
                # Also acknowledge pending pause/cancel before a delegate asks
                # for its next model response. Tool calls recheck individually.
                return await control_guard("read_file", {}) or await turn_stop_guard()

            policy = DelegationPolicy(
                scope_id=f"{project_ref.project_dir}:{run_id}:{run.attempt.current_attempt_id}",
                allowed_tools=frozenset(visible_tools),
                system_instructions=invocation.system_instructions,
                execution_guard=control_guard, stop_guard=delegation_stop_guard, run_id=run_id,
                read_paths=run_read_paths(run),
            )
            with (
                loop.tools.restrict_to(visible_tools, execution_guard=control_guard),
                loop.subagents.delegation_scope(policy),
                ExitStack() as read_access,
            ):
                for name in ("read_file", "list_dir", "grep", "glob"):
                    tool = loop.tools.get(name)
                    if tool is not None and hasattr(tool, "allow_read_paths"):
                        read_access.enter_context(tool.allow_read_paths(policy.read_paths))
                return await BaseAgentLoop._process_message(
                    loop,
                    prepared,
                    session_key=session_key,
                    on_progress=on_progress,
                    on_stream=on_stream,
                    on_stream_end=on_stream_end,
                    audit_hook=audit_hook,
                    completion_guard=completion_guard,
                    turn_stop_guard=turn_stop_guard,
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
