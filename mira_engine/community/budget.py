"""Community token budget and triage (design §10.3, mira#213).

The user decides how much the agent may spend on the community per day
(``community.daily_token_budget``); the engine spends it on what matters most:

* every community event gets a priority — things addressed to *this* agent
  (mentions, its own PRs, its work-order leases) first, then useful work
  (reviews, unanswered questions, work orders), then general discussion;
* as the day's budget runs down, lower priorities stop being picked up
  (below 50% only high and medium, below 20% only high, at 0 nothing);
* optionally a cheap model (``community.triage_model``) decides whether a
  low-priority event is worth a full turn, for a few hundred tokens.

Spending is recorded per day in ``community_budget.json`` and shows up in the
weekly harvest report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from loguru import logger

Priority = Literal["high", "medium", "low"]

_HIGH = frozenset(
    {
        "onboarding",
        "mention",
        "reply",
        "pr_reviewed",
        "pr_policy",
        "pr_rejected",
        "pr_reverted",
        "pr_merged",
        "work_order_lease",
        "work_order_cancelled",
    }
)
_MEDIUM = frozenset({"needs_review", "unanswered_question", "work_order_available", "summarize_thread"})

BUDGET_FILENAME = "community_budget.json"


def event_priority(event: dict[str, Any] | None) -> Priority:
    etype = str((event or {}).get("type") or "")
    if etype in _HIGH:
        return "high"
    if etype in _MEDIUM:
        return "medium"
    return "low"


def _budget_path() -> Path:
    from mira_engine.config.paths import get_data_dir

    return get_data_dir() / BUDGET_FILENAME


@dataclass
class BudgetDay:
    day: str
    tokens: int = 0
    turns: int = 0
    skipped: int = 0
    triage_tokens: int = 0


class CommunityBudget:
    """Per-day spend, persisted. ``limit`` 0 means unlimited."""

    def __init__(self, limit: int, path: Path | None = None):
        self.limit = max(0, int(limit or 0))
        self.path = path or _budget_path()

    def _load(self) -> dict[str, dict[str, int]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, dict[str, int]]) -> None:
        # Keep the last ~5 weeks.
        for key in sorted(data)[:-35]:
            data.pop(key, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as e:
            logger.debug("could not persist the community budget: {}", e)

    def today(self) -> BudgetDay:
        key = date.today().isoformat()
        return BudgetDay(key, **self._load().get(key, {}))

    def days(self) -> list[BudgetDay]:
        return [BudgetDay(k, **v) for k, v in sorted(self._load().items())]

    def _add(self, **delta: int) -> None:
        data = self._load()
        key = date.today().isoformat()
        day = data.setdefault(key, {})
        for k, v in delta.items():
            day[k] = int(day.get(k, 0)) + int(v)
        self._save(data)

    def record_turn(self, tokens: int) -> None:
        self._add(tokens=max(0, int(tokens or 0)), turns=1)

    def record_skip(self) -> None:
        self._add(skipped=1)

    def record_triage(self, tokens: int) -> None:
        self._add(tokens=max(0, int(tokens or 0)), triage_tokens=max(0, int(tokens or 0)))

    def remaining_fraction(self) -> float:
        if self.limit == 0:
            return 1.0
        return max(0.0, 1.0 - self.today().tokens / self.limit)

    def admits(self, priority: Priority) -> bool:
        left = self.remaining_fraction()
        if left <= 0.0:
            return False
        if left < 0.2:
            return priority == "high"
        if left < 0.5:
            return priority in ("high", "medium")
        return True


Triage = Callable[[str], Awaitable[tuple[bool, int]]]

TRIAGE_PROMPT = (
    "You triage community notifications for an AI agent with a limited budget. Answer with one word: "
    "RESPOND if the agent can add real value (it is asked something, it has relevant expertise, or "
    "the thread needs a specific answer), otherwise SKIP.\n\nAgent's interests: {interests}\n\n"
    "Notification:\n{event}"
)


async def should_handle(
    event: dict[str, Any] | None,
    content: str,
    budget: CommunityBudget,
    *,
    triage: Triage | None = None,
) -> tuple[bool, str]:
    """Decide whether a community event gets a full agent turn."""
    priority = event_priority(event)
    if not budget.admits(priority):
        budget.record_skip()
        return False, f"daily community budget: {priority}-priority events paused"
    if priority == "low" and triage is not None:
        try:
            respond, tokens = await triage(content)
        except Exception as e:  # noqa: BLE001 - triage failure → don't spend a full turn
            logger.debug("community triage failed: {}", e)
            respond, tokens = False, 0
        budget.record_triage(tokens)
        if not respond:
            budget.record_skip()
            return False, "triage: not worth a full turn"
    return True, priority
