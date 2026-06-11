"""Three-mode autonomy gate for community actions.

Modes (from CommunityConfig.autonomy_mode):
  - fully_autonomous: the agent acts directly on all community actions.
  - hitl (human-in-the-loop): every write action is held for human approval.
  - hybrid: low-impact actions (comment, vote) run automatically; high-impact
    ones (new proposal, code patch) are held for approval.

Held actions are appended to ~/.mira/community_approvals.jsonl so the desktop
app's approval inbox (#114) — or the human directly — can review them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from loguru import logger

from mira_engine.config.paths import get_data_dir

AutonomyMode = Literal["fully_autonomous", "hitl", "hybrid"]

# Impact tiers per action. Anything not listed defaults to "high".
_LOW_IMPACT = frozenset({"read_feed", "comment", "vote"})


class AutonomyGate:
    """Decide whether a community action may run now or needs human approval."""

    def __init__(self, mode: AutonomyMode):
        self.mode = mode

    def is_read_only(self, action: str) -> bool:
        return action == "read_feed"

    def requires_approval(self, action: str) -> bool:
        if self.is_read_only(action):
            return False
        if self.mode == "fully_autonomous":
            return False
        if self.mode == "hitl":
            return True
        # hybrid: hold only high-impact actions.
        return action not in _LOW_IMPACT

    @property
    def approvals_path(self):
        return get_data_dir() / "community_approvals.jsonl"

    def enqueue_approval(self, action: str, payload: dict[str, Any]) -> str:
        """Persist a pending action for later human review. Returns its id."""
        approval_id = f"{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        record = {
            "id": approval_id,
            "action": action,
            "payload": payload,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self.approvals_path
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Failed to write community approval: {}", e)
        logger.info("community action '{}' queued for approval ({})", action, approval_id)
        return approval_id
