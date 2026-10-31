"""Auto-mode Runs answer their own forms: the recommended choice wins after a short wait.

The deadline lives on the interaction (``interaction["auto_decide"]``), so it holds
whether or not a desktop is open and survives an engine restart. The UI only shows
the countdown and can cancel it; the engine submits the recommended choice and
starts the next turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from .models import ModuleRun, WorkflowEvent

if TYPE_CHECKING:  # service.py imports the helpers above
    from .service import WorkflowService

AUTO_DECIDE_SECONDS = 10
_BUSY_RETRY_SECONDS = 1.0


def auto_decides(run: ModuleRun) -> bool:
    """Experiment Runs in auto mode (the default) decide for an absent user."""
    return (
        run.module_type == "experiment"
        and run.module_data.get("experiment_execution_mode", "auto") != "manual"
    )


def recommended_choice(interaction_type: str, choices: list[dict[str, Any]]) -> dict[str, Any] | None:
    if interaction_type.startswith("approve_"):
        return next((choice for choice in choices if choice.get("value") == "approve"), None) or {
            "id": "approve", "label": "approve", "value": "approve",
        }
    marked = [choice for choice in choices if choice.get("recommended")]
    return marked[0] if len(marked) == 1 else None


def stamp_deadline(interaction: Any, *, now: datetime | None = None) -> None:
    """Start (or restart) the countdown of a pending auto decision."""
    if not isinstance(interaction, dict) or interaction.get("response") is not None:
        return
    auto = interaction.get("auto_decide")
    if not isinstance(auto, dict) or auto.get("cancelled_at"):
        return
    seconds = auto.get("seconds") if isinstance(auto.get("seconds"), int) else AUTO_DECIDE_SECONDS
    deadline = (now or datetime.now(UTC)) + timedelta(seconds=seconds)
    auto["deadline_at"] = deadline.isoformat().replace("+00:00", "Z")


def pending_auto_decision(run: ModuleRun) -> dict[str, Any] | None:
    if run.status != "waiting_input":
        return None
    interaction = run.module_data.get("interaction")
    if not isinstance(interaction, dict) or interaction.get("response") is not None:
        return None
    auto = interaction.get("auto_decide")
    if not isinstance(auto, dict) or auto.get("cancelled_at"):
        return None
    if not isinstance(auto.get("selection"), str) or not isinstance(auto.get("deadline_at"), str):
        return None
    return {
        "interaction_id": interaction.get("interaction_id"),
        "interaction_type": interaction.get("interaction_type"),
        "title": interaction.get("title"),
        "selection": auto["selection"],
        "label": auto.get("label") or auto["selection"],
        "deadline_at": auto["deadline_at"],
    }


def seconds_until(deadline_at: str, *, now: datetime | None = None) -> float:
    try:
        deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (deadline - (now or datetime.now(UTC))).total_seconds())


StartTurn = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class AutoDecider:
    """Arms one timer per waiting Run and answers with the recommendation when it fires."""

    def __init__(
        self,
        service_for: Callable[[str], WorkflowService],
        start_turn: StartTurn,
        *,
        is_busy: Callable[[str, str], bool] = lambda _project_id, _run_id: False,
    ) -> None:
        self._service_for = service_for
        self._start_turn = start_turn
        self._is_busy = is_busy
        self._timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def observe(self, event: WorkflowEvent) -> None:
        """Any committed event for a Run re-evaluates its timer."""
        if not event.run_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon(self.refresh, event.project_id, event.run_id)

    def refresh(self, project_id: str, run_id: str) -> None:
        self._cancel(project_id, run_id)
        try:
            run = self._service_for(project_id).repository.load_run(run_id)
        except Exception as exc:  # noqa: BLE001 - a missing or archived project just has no timer
            logger.debug("auto-decide: cannot load run {}/{}: {}", project_id, run_id, exc)
            return
        decision = pending_auto_decision(run)
        if decision:
            self._arm(project_id, run_id, decision["interaction_id"], seconds_until(decision["deadline_at"]))

    def rearm(self, project_ids: Iterable[str]) -> None:
        """Pick up countdowns that were pending when the engine stopped."""
        for project_id in project_ids:
            try:
                runs = self._service_for(project_id).repository.list_runs()
            except Exception as exc:  # noqa: BLE001
                logger.debug("auto-decide: cannot list runs of {}: {}", project_id, exc)
                continue
            for run in runs:
                decision = pending_auto_decision(run)
                if decision:
                    self._arm(project_id, run.run_id, decision["interaction_id"], seconds_until(decision["deadline_at"]))

    def close(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    def _cancel(self, project_id: str, run_id: str) -> None:
        handle = self._timers.pop((project_id, run_id), None)
        if handle:
            handle.cancel()

    def _arm(self, project_id: str, run_id: str, interaction_id: str, delay: float) -> None:
        self._cancel(project_id, run_id)
        loop = asyncio.get_running_loop()
        self._timers[(project_id, run_id)] = loop.call_later(
            delay, self._spawn, project_id, run_id, interaction_id
        )

    def _spawn(self, project_id: str, run_id: str, interaction_id: str) -> None:
        self._timers.pop((project_id, run_id), None)
        task = asyncio.get_running_loop().create_task(self.fire(project_id, run_id, interaction_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def fire(self, project_id: str, run_id: str, interaction_id: str) -> bool:
        """Submit the recommendation if the same form is still waiting; True when it did."""
        try:
            service = self._service_for(project_id)
            decision = pending_auto_decision(service.repository.load_run(run_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto-decide: cannot load run {}/{}: {}", project_id, run_id, exc)
            return False
        if not decision or decision["interaction_id"] != interaction_id:
            return False
        remaining = seconds_until(decision["deadline_at"])
        if remaining > 0:
            self._arm(project_id, run_id, interaction_id, remaining)
            return False
        from .service import WorkflowError

        if self._is_busy(project_id, run_id):
            # The turn that asked is still winding down; answer once it has ended.
            self._arm(project_id, run_id, interaction_id, _BUSY_RETRY_SECONDS)
            return False
        try:
            receipt = service.command_run(
                run_id,
                command_id=f"auto-decide:{interaction_id}",
                command_type="submit_interaction",
                payload={
                    "interaction_id": interaction_id,
                    "response": {"selection": decision["selection"]},
                    "auto_decided": True,
                },
            )
        except WorkflowError as exc:
            # The user answered (or the Run moved on) first.
            logger.info("auto-decide skipped for {}/{}: {}", project_id, run_id, exc)
            return False
        if receipt.disposition == "duplicate":
            return False
        logger.info(
            "auto-decide: {}/{} answered {!r} with the recommended {!r}",
            project_id, run_id, decision["interaction_type"], decision["selection"],
        )
        try:
            await self._start_turn(project_id, run_id, decision)
        except Exception:  # noqa: BLE001 - the answer is saved; a chat message resumes the Run
            logger.exception("auto-decide: could not start the next turn for {}/{}", project_id, run_id)
        return True
