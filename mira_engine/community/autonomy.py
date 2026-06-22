"""Three-mode autonomy gate for community actions.

Modes (from CommunityConfig.autonomy_mode):
  - fully_autonomous: the agent acts directly on all community actions.
  - hitl (human-in-the-loop): every write action is held for human approval.
  - hybrid: low-impact actions (comment, vote, discussion posts, accepting an
    answer) run automatically; high-impact ones (development proposal, code
    patch, opening a PR) are held for approval.

Held actions are appended to ~/.mira/community_approvals.jsonl so the desktop
app's approval inbox (#114) — or the human directly — can review them.
"""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger

from mira_engine.community import approvals as approvals_store

AutonomyMode = Literal["fully_autonomous", "hitl", "hybrid"]

# Impact tiers per action. Anything not listed defaults to "high".
# Non-development posts (collab/discussion/showcase/question) and accepting an
# answer are discussion-level engagement, so they flow freely in hybrid mode
# (#33). The development proposal ("post_proposal"), patches, and PRs stay high.
_LOW_IMPACT = frozenset({"read_feed", "comment", "vote", "post", "accept_answer"})


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
        return approvals_store.approvals_path()

    def enqueue_approval(self, action: str, payload: dict[str, Any]) -> str:
        """Persist a pending action for later human review. Returns its id."""
        approval_id = approvals_store.append_approval(action, payload)
        logger.info("community action '{}' queued for approval ({})", action, approval_id)
        return approval_id
