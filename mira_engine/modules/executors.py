"""Concrete native executors for the four canonical MIRA modules."""

from __future__ import annotations

import json
from abc import ABC
from typing import ClassVar

from mira_engine.workflows.models import ModuleType

from .base import (
    ExecutorDescriptor,
    ModuleExecutionContext,
    ModuleInvocation,
    ModuleInvoker,
    ValidationResult,
)
from .prompts import module_system_instructions

_LEGACY_OR_OUT_OF_BAND_TOOLS = frozenset({"set_plan", "cron", "message"})


class NativeModuleExecutor(ABC):
    """Module policy around the shared, workflow-neutral agent runtime."""

    descriptor: ClassVar[ExecutorDescriptor]
    blocked_tools: ClassVar[frozenset[str]] = _LEGACY_OR_OUT_OF_BAND_TOOLS
    operating_contract: ClassVar[str] = ""

    async def validate_inputs(self, context: ModuleExecutionContext) -> ValidationResult:
        errors: list[str] = []
        if context.run.module_type != self.descriptor.module_type:
            errors.append(
                f"executor {self.descriptor.executor_name} cannot run {context.run.module_type}"
            )
        available = {artifact.artifact_id: artifact for artifact in context.artifacts}
        for selected in context.run.inputs:
            artifact = available.get(selected.artifact_id)
            if artifact is None:
                errors.append(f"selected Artifact is unavailable: {selected.artifact_id}")
                continue
            if selected.expected_type and artifact.artifact_type != selected.expected_type:
                errors.append(
                    f"Artifact {selected.artifact_id} has type "
                    f"{artifact.artifact_type}, expected {selected.expected_type}"
                )
        has_intent = isinstance(context.run.module_data.get("intent"), str) and bool(
            str(context.run.module_data["intent"]).strip()
        )
        has_resolved_interaction = (
            isinstance(context.run.module_data.get("interaction"), dict)
            and context.run.module_data["interaction"].get("response") is not None
        )
        if (
            not context.user_content.strip()
            and not has_intent
            and not context.run.inputs
            and not has_resolved_interaction
        ):
            errors.append("a user brief, selected Artifact, or resolved interaction is required")
        return ValidationResult(valid=not errors, errors=tuple(errors))

    def system_instructions(self, context: ModuleExecutionContext) -> str:
        artifact_context = [
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "label": artifact.label,
                "media_type": artifact.media_type,
                "payload_ref": artifact.payload_ref,
                "sha256": artifact.sha256,
            }
            for artifact in context.artifacts
        ]
        run_context = {
            "run_id": context.run.run_id,
            "module_type": context.run.module_type,
            "intent": context.run.module_data.get("intent"),
            "parent_run_id": context.run.module_data.get("parent_run_id"),
            "handoff_summary": context.run.module_data.get("handoff_summary"),
            "selected_artifacts": artifact_context,
        }
        interaction = context.run.module_data.get("interaction")
        if isinstance(interaction, dict) and interaction.get("response") is not None:
            run_context["resolved_interaction"] = {
                "interaction_id": interaction.get("interaction_id"),
                "interaction_type": interaction.get("interaction_type"),
                "response": interaction.get("response"),
            }
        return (
            f"{module_system_instructions(context.run.module_type, context.run.run_id)}\n\n"
            "Native executor contract:\n"
            f"{self.operating_contract}\n\n"
            "The following JSON is the authoritative Run input. Do not infer hidden "
            "inputs from another Run or from task_plan.json:\n"
            f"{json.dumps(run_context, ensure_ascii=False, indent=2)}"
        )

    async def execute(
        self,
        context: ModuleExecutionContext,
        invoke: ModuleInvoker,
    ):
        validation = await self.validate_inputs(context)
        if not validation.valid:
            raise ValueError("; ".join(validation.errors))
        return await invoke(
            ModuleInvocation(
                system_instructions=self.system_instructions(context),
                blocked_tools=self.blocked_tools,
            )
        )


class InitiationExecutor(NativeModuleExecutor):
    descriptor = ExecutorDescriptor(
        module_type="initiation",
        executor_name="mira.initiation",
        executor_version="1.0.0",
    )
    blocked_tools = _LEGACY_OR_OUT_OF_BAND_TOOLS | frozenset(
        {"exec", "bg", "spawn", "consult_role"}
    )
    operating_contract = (
        "Own only question clarification, evidence/literature investigation, gap "
        "analysis, and research-direction choices. Never create a legacy plan or "
        "execute experiments. Use request_workflow_interaction for a direction choice."
    )


class PlanningExecutor(NativeModuleExecutor):
    descriptor = ExecutorDescriptor(
        module_type="planning",
        executor_name="mira.planning",
        executor_version="1.0.0",
    )
    blocked_tools = _LEGACY_OR_OUT_OF_BAND_TOOLS | frozenset(
        {"exec", "bg", "spawn", "consult_role"}
    )
    operating_contract = (
        "Own only requirements, hypotheses, risks, acceptance criteria, and an "
        "explicitly approved executable plan. Never call set_plan or mutate "
        "task_plan.json. Never execute the proposed experiment."
    )


class ExperimentExecutor(NativeModuleExecutor):
    descriptor = ExecutorDescriptor(
        module_type="experiment",
        executor_name="mira.experiment",
        executor_version="1.0.0",
    )
    blocked_tools = _LEGACY_OR_OUT_OF_BAND_TOOLS
    operating_contract = (
        "Own validation and execution of the supplied experiment scope. Preserve "
        "existing command, path, resource, and confirmation guardrails. Record "
        "methods and evidence in Run-local files or immutable Artifacts; never "
        "update the legacy task_plan workflow."
    )


class DeliveryExecutor(NativeModuleExecutor):
    descriptor = ExecutorDescriptor(
        module_type="delivery",
        executor_name="mira.delivery",
        executor_version="1.0.0",
    )
    blocked_tools = _LEGACY_OR_OUT_OF_BAND_TOOLS | frozenset({"spawn", "consult_role"})
    operating_contract = (
        "Own only analysis, writing, polishing, visualization, packaging, and "
        "export of the explicitly selected material. Do not launch upstream "
        "research or experiments and do not use task_plan.json as hidden input."
    )


class ModuleExecutorRegistry:
    def __init__(self) -> None:
        executors = (
            InitiationExecutor(),
            PlanningExecutor(),
            ExperimentExecutor(),
            DeliveryExecutor(),
        )
        self._executors = {executor.descriptor.module_type: executor for executor in executors}

    def get(self, module_type: ModuleType) -> NativeModuleExecutor:
        return self._executors[module_type]

    @property
    def module_types(self) -> tuple[ModuleType, ...]:
        return tuple(self._executors)  # type: ignore[return-value]
