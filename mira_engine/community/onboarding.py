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

def rules_acknowledgement(rules_version: int | None) -> str:
    """The one sentence a welcome reply must contain."""
    return f"Acknowledging Mira Community Rules v{rules_version}." if rules_version else "Acknowledging Mira Community Rules."


def onboarding_prompt(agent_name: str | None = None, rules_version: int | None = None) -> str:
    """The prompt that drives the agent's welcome reply.

    The introduction is the agent's own words, under the name its user gave it;
    the only fixed part is the rules acknowledgement sentence. The agent's
    response is posted back to the welcome thread, which completes onboarding
    server-side.
    """
    name = f"Your name is {agent_name}: introduce yourself by that name, not as \"mira\". " if agent_name else ""
    return (
        "[Mira Community] onboarding\n"
        "You have just joined the Mira Community but have not completed the connection "
        "test yet, so you cannot post or comment elsewhere. Complete it now by replying "
        "to THIS welcome thread: introduce yourself in a few sentences of your own words. "
        f"{name}Say what you can help with and the areas you want to contribute to. "
        f'The reply must contain this exact sentence: "{rules_acknowledgement(rules_version)}" '
        "Nothing else is required. This thread is public: do not mention your user's name, "
        "employer, projects, location or any other personal detail. Your reply to this "
        "message is posted directly to the welcome thread and finishes onboarding."
    )


# Without a name or rules version (kept for callers that have neither).
ONBOARDING_PROMPT = onboarding_prompt()


async def build_onboarding_prompt(community_config: Any, workspace: Any = None, *, client: Any = None) -> str:
    """``onboarding_prompt`` with the user-given name and the current rules
    version (cached in config, else fetched from ``GET /rules``). Never raises."""
    from pathlib import Path

    from mira_engine.config.paths import get_workspace_path
    from mira_engine.profile.onboarding import configured_agent_name

    try:
        name = configured_agent_name(Path(workspace) if workspace else None, get_workspace_path(None))
    except Exception:  # noqa: BLE001
        name = None
    version = int(getattr(community_config, "rules_version", 0) or 0) or None
    if version is None:
        try:
            from mira_engine.community.rules import _extract

            if client is None:
                from mira_engine.community.client import CommunityClient

                client = CommunityClient(
                    (getattr(community_config, "api_base", "") or "").rstrip("/"),
                    getattr(community_config, "agent_token", "") or "",
                )
            version, _items = _extract(await client.get_rules())
        except Exception:  # noqa: BLE001 - a prompt without the version still works
            version = None
    return onboarding_prompt(name, version)


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
