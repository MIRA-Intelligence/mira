"""Concrete native executors for the four canonical MIRA modules."""

from __future__ import annotations

import json
from abc import ABC
from typing import ClassVar

from mira_engine.workflows.confirmations import confirmation_context
from mira_engine.workflows.models import ModuleType

from .base import (
    ExecutorDescriptor,
    ModuleExecutionContext,
    ModuleInvocation,
    ModuleInvoker,
    ValidationResult,
)
from .profiles import profile_allows_team_tools, profile_system_instructions
from .prompts import module_confirmation_instructions, module_system_instructions

_LEGACY_OR_OUT_OF_BAND_TOOLS = frozenset(
    {"set_plan", "cron", "message", "consult", "spawn"}
)
_INITIATION_ONLY_TOOLS = frozenset(
    {"record_initiation_research_result", "pubmed_search"}
)
_INTERNAL_MODULE_DATA_KEYS = frozenset(
    {
        "intent",
        "parent_run_id",
        "handoff_summary",
        "interaction",
        "interaction_history",
        "confirmation_policy",
        "recovery",
        "failure",
        "completion_summary",
        "suggested_handoffs",
        "auto_handoff",
        "handoff_not_before",
    }
)


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
        confirmation = confirmation_context(context.run)
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
            "agent_profile": context.agent_profile,
            "intent": context.run.module_data.get("intent"),
            "parent_run_id": context.run.module_data.get("parent_run_id"),
            "handoff_summary": context.run.module_data.get("handoff_summary"),
            "confirmation": confirmation,
            "selected_artifacts": artifact_context,
            "configuration": {
                key: value
                for key, value in context.run.module_data.items()
                if key not in _INTERNAL_MODULE_DATA_KEYS
            },
        }
        interaction = context.run.module_data.get("interaction")
        if isinstance(interaction, dict) and interaction.get("response") is not None:
            run_context["resolved_interaction"] = {
                "interaction_id": interaction.get("interaction_id"),
                "interaction_type": interaction.get("interaction_type"),
                "title": interaction.get("title"),
                "prompt": interaction.get("prompt"),
                "choices": interaction.get("choices", []),
                "response": interaction.get("response"),
            }
        interaction_history = context.run.module_data.get("interaction_history")
        if isinstance(interaction_history, list):
            run_context["resolved_interactions"] = [
                {
                    "interaction_id": item.get("interaction_id"),
                    "interaction_type": item.get("interaction_type"),
                    "title": item.get("title"),
                    "prompt": item.get("prompt"),
                    "choices": item.get("choices", []),
                    "response": item.get("response"),
                    "resolved_at": item.get("resolved_at"),
                }
                for item in interaction_history
                if isinstance(item, dict) and item.get("response") is not None
            ]
        return (
            f"{module_system_instructions(context.run.module_type, context.run.run_id)}\n\n"
            f"{module_confirmation_instructions(context.run.module_type, **confirmation)}\n\n"
            "Native executor contract:\n"
            f"{self.operating_contract}\n\n"
            "The following JSON is the authoritative Run input. Do not infer hidden "
            "inputs from another Run or from task_plan.json:\n"
            f"{json.dumps(run_context, ensure_ascii=False, indent=2)}\n\n"
            f"{profile_system_instructions(context.agent_profile)}"
        )

    def blocked_tools_for(self, context: ModuleExecutionContext) -> frozenset[str]:
        blocked = self.blocked_tools
        if context.run.module_type != "experiment":
            blocked = blocked | {"record_workflow_experiment"}
        if profile_allows_team_tools(context.agent_profile):
            blocked = blocked - {"consult", "spawn"}
        return blocked

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
                blocked_tools=self.blocked_tools_for(context),
            )
        )


class InitiationExecutor(NativeModuleExecutor):
    descriptor = ExecutorDescriptor(
        module_type="initiation",
        executor_name="mira.initiation",
        executor_version="1.1.0",
    )
    blocked_tools = _LEGACY_OR_OUT_OF_BAND_TOOLS | frozenset({"exec", "bg"})
    operating_contract = (
        "Own only question clarification, evidence/literature investigation, gap "
        "analysis, and research-direction choices. Never create a legacy plan or "
        "execute experiments. Use request_workflow_interaction for output-direction "
        "clarification before research when needed, and again for material decisions "
        "during research or a researched direction choice. A pending clarification "
        "is a valid stopping boundary even before any search or research result. "
        "Honor configuration.literature_review: when disabled, do not perform external "
        "literature search; when enabled, use only its selected sources where available. "
        "A statement "
        "such as 'I am starting the search' is only a progress update, never a valid end "
        "of the turn. Perform the searches in this same execution. When PubMed is selected, "
        "you MUST call pubmed_search with explicit queries; never substitute model memory "
        "or a generic summary. For Google Scholar use web_search with site:scholar.google.com "
        "(falling back to a general query only if blocked); for arXiv, Semantic Scholar, "
        "Crossref, and Europe PMC use web_search/web_fetch restricted to the selected "
        "domains. Use 3-6 focused queries with at most 10 results each, then stop searching "
        "and synthesize; do not exhaust context with repetitive variants. Decompose the "
        "question into multiple searches, inspect returned metadata "
        "and abstracts, and keep only verifiable papers. Put the clean PubMed/source URL "
        "in url and the bare DOI in doi; never concatenate them into one field. "
        "Treat configuration.references as user-selected priority evidence. After "
        "synthesizing evidence, call record_initiation_research_result with the survey, "
        "complete paper metadata, notes, gaps, and candidate directions when requested "
        "(directions may be empty for an overview-only outcome). Do this before "
        "request_workflow_interaction asks the user to choose a direction, and update it "
        "again whenever the research result materially changes."
    )


class PlanningExecutor(NativeModuleExecutor):
    descriptor = ExecutorDescriptor(
        module_type="planning",
        executor_name="mira.planning",
        executor_version="1.0.0",
    )
    blocked_tools = (
        _LEGACY_OR_OUT_OF_BAND_TOOLS
        | _INITIATION_ONLY_TOOLS
        | frozenset({"exec", "bg"})
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
    blocked_tools = _LEGACY_OR_OUT_OF_BAND_TOOLS | _INITIATION_ONLY_TOOLS
    operating_contract = (
        "Own validation and execution of the supplied experiment scope. Preserve "
        "existing command, path, resource, and confirmation guardrails. Record "
        "methods and evidence in Run-local files or immutable Artifacts; never "
        "update the legacy task_plan workflow. Honor configuration.automation_policy: "
        "maxExperiments is a hard cap, maxTokens is a token budget, and goals are "
        "stopping conditions combined by its AND/OR logic."
    )


class DeliveryExecutor(NativeModuleExecutor):
    descriptor = ExecutorDescriptor(
        module_type="delivery",
        executor_name="mira.delivery",
        executor_version="1.0.0",
    )
    blocked_tools = _LEGACY_OR_OUT_OF_BAND_TOOLS | _INITIATION_ONLY_TOOLS
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
