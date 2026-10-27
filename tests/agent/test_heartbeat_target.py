"""Where a heartbeat turn runs and where its result goes (heartbeat/target.py).

Background work never lands in the chat the user happens to be using.
"""

from __future__ import annotations

from mira_engine.community.chat import COMMUNITY_CHAT_ID
from mira_engine.heartbeat.target import (
    HEARTBEAT_SESSION_KEY,
    HeartbeatTarget,
    parse_deliver_to,
    pick_heartbeat_target,
)

# The user's most recent conversation is a UI chat they are working in.
RECENT = ["ui:chat-working-in", "community:thread-1", "telegram:4242", "cli:direct", "system:x"]


def test_the_desktop_ui_gets_the_reserved_community_chat_not_the_recent_one():
    target = pick_heartbeat_target(enabled_channels=["ui", "community"], recent_session_keys=RECENT)
    assert target == HeartbeatTarget("ui", COMMUNITY_CHAT_ID, session_key=f"ui:{COMMUNITY_CHAT_ID}", dedicated=True)
    assert target.deliverable


def test_the_reserved_chat_wins_over_a_messaging_channel_used_more_recently():
    target = pick_heartbeat_target(
        enabled_channels=["telegram", "ui"], recent_session_keys=["telegram:4242", "ui:chat-a"]
    )
    assert (target.channel, target.chat_id) == ("ui", COMMUNITY_CHAT_ID)


def test_deliver_to_names_the_users_own_conversation_in_the_heartbeats_session():
    target = pick_heartbeat_target(
        enabled_channels=["ui", "telegram"], recent_session_keys=RECENT, deliver_to="telegram:777"
    )
    assert target == HeartbeatTarget("telegram", "777", session_key=HEARTBEAT_SESSION_KEY, dedicated=False)


def test_deliver_to_a_ui_chat_makes_that_chat_the_dedicated_one():
    target = pick_heartbeat_target(enabled_channels=["ui"], recent_session_keys=[], deliver_to="ui:my-desk")
    assert target == HeartbeatTarget("ui", "my-desk", session_key="ui:my-desk", dedicated=True)


def test_deliver_to_on_a_channel_that_is_off_is_ignored():
    target = pick_heartbeat_target(
        enabled_channels=["ui"], recent_session_keys=RECENT, deliver_to="telegram:777"
    )
    assert (target.channel, target.chat_id) == ("ui", COMMUNITY_CHAT_ID)


def test_without_the_ui_the_most_recent_messaging_conversation_is_used():
    target = pick_heartbeat_target(
        enabled_channels=["telegram", "community"],
        recent_session_keys=["community:thread-1", "cli:direct", "system:x", "feishu:9", "telegram:4242"],
    )
    # community/cli/system sessions are never a target; feishu is not enabled.
    assert target == HeartbeatTarget("telegram", "4242", session_key=HEARTBEAT_SESSION_KEY)
    assert not target.dedicated


def test_nowhere_to_deliver_keeps_the_turn_in_its_own_session():
    target = pick_heartbeat_target(enabled_channels=["community"], recent_session_keys=RECENT)
    assert target == HeartbeatTarget("cli", "direct", session_key=HEARTBEAT_SESSION_KEY)
    assert not target.deliverable


def test_parse_deliver_to():
    assert parse_deliver_to("telegram:4242") == ("telegram", "4242")
    assert parse_deliver_to(" ui : community ") == ("ui", "community")
    assert parse_deliver_to("ui:a:b") == ("ui", "a:b")
    for malformed in ("", "telegram", ":4242", "ui:", None):
        assert parse_deliver_to(malformed) is None  # type: ignore[arg-type]
