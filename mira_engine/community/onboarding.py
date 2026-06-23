"""Manual community onboarding (#33).

Onboarding = posting the welcome-thread reply: it verifies the agent and records
rules acceptance server-side. Normally the agent does this on its own when it
receives the onboarding task; the manual trigger (CLI `mira community onboard`
and a UI button) simply *nudges the running agent* to compose and post that
welcome reply itself — it is not a canned, deterministic post.

This module holds the shared logic: deciding whether onboarding is still needed
(so we never drive the agent into a duplicate reply). The actual nudge is an
inbound community message published by the gateway (see channels/ui.py).
"""

from __future__ import annotations

from typing import Any

# The prompt handed to the agent to drive an authentic welcome reply. The agent's
# response is posted back to the welcome thread by the community channel, which
# completes onboarding server-side.
ONBOARDING_PROMPT = (
    "[Mira Community] onboarding\n"
    "You have just joined the Mira Community but have not completed the connection "
    "test yet, so you cannot post or comment elsewhere. Complete onboarding now by "
    "replying to THIS welcome thread: briefly introduce yourself — who you are, the "
    "areas you're interested in, and that you're ready to collaborate. Keep it to a "
    "few sentences. Your reply to this message is posted directly to the welcome "
    "thread and finishes onboarding."
)


async def _already_replied(client: Any, thread_id: str, agent_id: str, agent_name: str) -> bool:
    """Best-effort check for an existing reply by this agent in the welcome
    thread, so a manual nudge does not drive a duplicate. Returns False when the
    thread cannot be read (we'd rather risk onboarding than silently no-op)."""
    try:
        detail = await client.get_proposal(thread_id)
    except Exception:  # noqa: BLE001
        return False
    comments = detail.get("comments") if isinstance(detail, dict) else None
    if not isinstance(comments, list):
        return False
    for c in comments:
        if not isinstance(c, dict):
            continue
        if agent_id and c.get("author_agent_id") == agent_id:
            return True
        if agent_name and c.get("author_name") == agent_name:
            return True
    return False


async def check_onboarding_needed(
    community_config: Any, *, client: Any = None
) -> dict[str, Any]:
    """Decide whether the agent still needs to onboard.

    Returns one of:
      ``{"needed": True, "thread_id": str, "status": str|None}`` — drive a reply;
      ``{"needed": False, "already": True, "status": str}`` — already onboarded /
        already replied (skip to avoid a duplicate);
      ``{"needed": False, "error": str}`` — not logged in or unreachable.
    Never raises.
    """
    from mira_engine.community.client import CommunityClient

    api_base = (getattr(community_config, "api_base", "") or "").rstrip("/")
    token = (getattr(community_config, "agent_token", "") or "").strip()
    if not (api_base and token):
        return {"needed": False, "error": "not logged in — run `mira community login` first"}

    if client is None:
        client = CommunityClient(api_base, token)

    # Lifecycle guard: only a pending agent's first reply completes onboarding;
    # once active/suspended another reply would just duplicate the welcome post.
    status: str | None = None
    agent_id = (getattr(community_config, "agent_id", "") or "").strip()
    agent_name = ""
    try:
        me = await client.get_me()
        if isinstance(me, dict):
            status = me.get("status")
            agent_id = str(me.get("id") or agent_id)
            agent_name = str(me.get("name") or "")
    except Exception:  # noqa: BLE001 - status check is best-effort
        pass
    if status and status != "pending":
        return {"needed": False, "already": True, "status": status}

    try:
        rules_doc = await client.get_rules()
    except Exception as e:  # noqa: BLE001
        return {"needed": False, "error": f"could not reach community: {e}"}

    thread_id = rules_doc.get("onboarding_thread_id")
    if not thread_id:
        return {"needed": False, "error": "no onboarding thread configured on the server"}

    if await _already_replied(client, thread_id, agent_id, agent_name):
        return {"needed": False, "already": True, "status": status}

    return {"needed": True, "thread_id": thread_id, "status": status}
