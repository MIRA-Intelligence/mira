from __future__ import annotations

from typing import Any, cast

from mira_engine.workflows.confirmations import confirmation_approved
from mira_engine.workflows.models import ModuleRun


def _run(module_data: dict[str, Any]) -> ModuleRun:
    return cast(
        ModuleRun,
        type(
            "ConfirmationRun",
            (),
            {"module_type": "experiment", "module_data": module_data},
        )(),
    )


def test_latest_confirmation_decision_wins_over_an_older_approval() -> None:
    run = _run(
        {
            "confirmation_policy": {"mode": "required"},
            "interaction_history": [
                {
                    "interaction_type": "approve_experiment_plan",
                    "response": {"selection": "approve"},
                },
                {
                    "interaction_type": "approve_experiment_plan",
                    "response": {"selection": "revise", "notes": "Reduce the budget"},
                },
            ],
        }
    )

    assert confirmation_approved(run) is False


def test_pending_current_confirmation_supersedes_older_approval() -> None:
    run = _run(
        {
            "confirmation_policy": "required",
            "interaction_history": [
                {
                    "interaction_type": "approve_experiment_plan",
                    "response": {"selection": "approve"},
                }
            ],
            "interaction": {
                "interaction_type": "approve_experiment_plan",
            },
        }
    )

    assert confirmation_approved(run) is False
