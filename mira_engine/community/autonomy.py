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
# (#33), as does a code review (it only informs the merge gate,
# mira-community#45). Renewing or giving back a work-order lease only affects
# the agent's own claim. The development proposal ("post_proposal"), claiming
# a work order, patches, and PRs stay high.
_LOW_IMPACT = frozenset(
    {"read_feed", "comment", "vote", "review", "post", "accept_answer", "renew_work_order", "release_work_order"}
)


_MODES = ("fully_autonomous", "hitl", "hybrid")


def live_autonomy_mode() -> AutonomyMode | None:
    """The mode saved in config.json right now (None if unreadable).

    Reads just this field from the file (no full config load), so checking it
    on every community write is cheap and logs nothing.
    """
    import json

    from mira_engine.config.loader import get_config_path

    try:
        data = json.loads(get_config_path().expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    community = data.get("community") if isinstance(data, dict) else None
    if not isinstance(community, dict):
        return None
    mode = community.get("autonomyMode") or community.get("autonomy_mode")
    return mode if mode in _MODES else None


class AutonomyGate:
    """Decide whether a community action may run now or needs human approval.

    With ``live=True`` the mode is read from config.json on every check, so a
    change made in the desktop app or with ``mira community`` applies to the
    agent's next action without restarting the gateway. ``mode`` is then the
    fallback when the file cannot be read.
    """

    def __init__(self, mode: AutonomyMode, *, live: bool = False):
        self._mode = mode
        self._live = live

    @property
    def mode(self) -> AutonomyMode:
        if self._live:
            current = live_autonomy_mode()
            if current is not None:
                return current
        return self._mode

    @mode.setter
    def mode(self, value: AutonomyMode) -> None:
        self._mode = value

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

    def enqueue_approval(
        self, action: str, payload: dict[str, Any], origin: dict[str, str] | None = None
    ) -> str:
        """Persist a pending action for later human review. Returns its id."""
        approval_id = approvals_store.append_approval(action, payload, origin)
        logger.info("community action '{}' queued for approval ({})", action, approval_id)
        return approval_id
