"""Pure helpers for module-local AI confirmation gates."""

from __future__ import annotations

from typing import Any

from .models import ModuleRun, ModuleType

CONFIRMATION_INTERACTION_TYPES: dict[ModuleType, str] = {
    "initiation": "approve_research_scope",
    "planning": "approve_plan",
    "experiment": "approve_experiment_plan",
    "delivery": "approve_delivery_brief",
}


def confirmation_required(run: ModuleRun) -> bool:
    policy = run.module_data.get("confirmation_policy")
    if isinstance(policy, str):
        return policy == "required"
    return isinstance(policy, dict) and policy.get("mode") == "required"


def expected_confirmation_type(module_type: ModuleType) -> str:
    return CONFIRMATION_INTERACTION_TYPES[module_type]


def _interaction_approved(interaction: Any, expected_type: str) -> bool:
    if not isinstance(interaction, dict) or interaction.get("interaction_type") != expected_type:
        return False
    response = interaction.get("response")
    if isinstance(response, str):
        return response == "approve"
    if not isinstance(response, dict):
        return False
    decision = response.get("selection", response.get("decision", response.get("approval")))
    return decision == "approve"


def confirmation_approved(run: ModuleRun) -> bool:
    if not confirmation_required(run):
        return True
    expected_type = expected_confirmation_type(run.module_type)
    current = run.module_data.get("interaction")
    if isinstance(current, dict) and current.get("interaction_type") == expected_type:
        return _interaction_approved(current, expected_type)
    history = run.module_data.get("interaction_history")
    if not isinstance(history, list):
        return False
    latest = next(
        (
            interaction
            for interaction in reversed(history)
            if isinstance(interaction, dict)
            and interaction.get("interaction_type") == expected_type
        ),
        None,
    )
    return _interaction_approved(latest, expected_type)


def confirmation_context(run: ModuleRun) -> dict[str, Any]:
    required = confirmation_required(run)
    return {
        "required": required,
        "interaction_type": expected_confirmation_type(run.module_type),
        "approved": confirmation_approved(run) if required else True,
    }
