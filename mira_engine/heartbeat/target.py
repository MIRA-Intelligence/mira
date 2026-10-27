"""Where a heartbeat turn runs, and where what it says goes.

A background turn never lands in whatever chat the user happens to be using:
that polluted the conversation they were in, and left its context split (the
turn's own memory lived under a ``heartbeat`` key, the message in their chat).

The rules, first match wins:

1. ``gateway.heartbeat.deliver_to`` (``"channel:chat_id"``), when that channel
   is on. A UI chat named this way is treated as dedicated to background work.
2. The desktop UI is on: the reserved Mira Community chat
   (:data:`~mira_engine.community.chat.COMMUNITY_CHAT_ID`). The turn runs in
   that chat's own session, so the user can talk back there.
3. Otherwise the most recent conversation on a messaging channel (a DM has no
   chat list to pollute), in the heartbeat's own session.
4. Nothing to deliver to: the turn still runs; its result stays in its session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mira_engine.community.chat import COMMUNITY_CHAT_ID

#: The session of a background turn that is delivered into someone's own
#: conversation: kept apart from that conversation's history.
HEARTBEAT_SESSION_KEY = "heartbeat"

# Sessions that are never a delivery target: the CLI, internal system turns,
# and the community channel (its ``send`` only posts the welcome reply).
_NOT_A_TARGET = frozenset({"cli", "system", "community", "ui"})


@dataclass(frozen=True)
class HeartbeatTarget:
    channel: str
    chat_id: str
    #: The agent's session for the turn. In a dedicated chat it is the chat's
    #: own session, shared with the user's messages there.
    session_key: str
    #: A chat that exists for background work: everything the turn says is
    #: delivered there, without the "worth interrupting the user?" filter.
    dedicated: bool = False

    @property
    def deliverable(self) -> bool:
        return self.channel != "cli"


def parse_deliver_to(value: str) -> tuple[str, str] | None:
    """``"channel:chat_id"`` → ``(channel, chat_id)``; ``None`` when malformed."""
    raw = (value or "").strip()
    if ":" not in raw:
        return None
    channel, chat_id = (part.strip() for part in raw.split(":", 1))
    if not channel or not chat_id:
        return None
    return channel, chat_id


def _ui_target(chat_id: str) -> HeartbeatTarget:
    return HeartbeatTarget("ui", chat_id, session_key=f"ui:{chat_id}", dedicated=True)


def pick_heartbeat_target(
    *,
    enabled_channels: Iterable[str],
    recent_session_keys: Iterable[str],
    deliver_to: str = "",
) -> HeartbeatTarget:
    """Choose the target for this tick (see the module docstring for the rules).

    ``recent_session_keys`` are ``"channel:chat_id"`` keys, most recent first.
    """
    enabled = set(enabled_channels)

    configured = parse_deliver_to(deliver_to)
    if configured and configured[0] in enabled:
        channel, chat_id = configured
        if channel == "ui":
            return _ui_target(chat_id)
        return HeartbeatTarget(channel, chat_id, session_key=HEARTBEAT_SESSION_KEY)

    if "ui" in enabled:
        return _ui_target(COMMUNITY_CHAT_ID)

    for key in recent_session_keys:
        if ":" not in key:
            continue
        channel, chat_id = key.split(":", 1)
        if channel in _NOT_A_TARGET or not chat_id or channel not in enabled:
            continue
        return HeartbeatTarget(channel, chat_id, session_key=HEARTBEAT_SESSION_KEY)

    return HeartbeatTarget("cli", "direct", session_key=HEARTBEAT_SESSION_KEY)
