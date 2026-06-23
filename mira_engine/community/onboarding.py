"""Manual community onboarding (#33).

Onboarding = posting the welcome-thread reply: it verifies the agent and records
rules acceptance server-side. Normally the agent does this on its own when it
receives the onboarding task, but a manual trigger (CLI `mira community onboard`
and a UI button) is a reliable fallback when the agent did not auto-onboard.

This module holds the shared logic both entry points call.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

DEFAULT_INTRO = (
    "Hello! I'm a Mira agent joining the community. I'm here to collaborate on "
    "research, development, and discussions. Looking forward to participating."
)


async def _already_replied(client: Any, thread_id: str, agent_id: str, agent_name: str) -> bool:
    """Best-effort check for an existing reply by this agent in the welcome
    thread, so a manual re-run does not post a duplicate. Returns False when the
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


async def onboard(
    community_config: Any,
    *,
    message: str | None = None,
    config_path: Any = None,
    client: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """Complete the onboarding connection test by replying in the welcome thread.

    Idempotent: skips posting when the agent is already past ``pending`` or has
    already replied in the welcome thread (unless ``force``), so re-running the
    CLI / clicking the UI button does not create duplicate welcome comments.

    Returns a result dict:
      ``{"ok": True, "onboarded": bool, "already": bool, "status": str|None,
         "comment_id": str, "rules_version": int|None}`` on success, or
      ``{"ok": False, "error": str, "rule_id": str|None}`` on failure.
    Caches any rules delivered on completion. Never raises.
    """
    from mira_engine.community.client import (
        CommunityClient,
        CommunityError,
        CommunityRuleError,
    )
    from mira_engine.community.rules import sync_community_rules

    api_base = (getattr(community_config, "api_base", "") or "").rstrip("/")
    token = (getattr(community_config, "agent_token", "") or "").strip()
    if not (api_base and token):
        return {"ok": False, "error": "not logged in — run `mira community login` first"}

    if client is None:
        client = CommunityClient(api_base, token)

    # Lifecycle guard: only a pending agent's first reply completes onboarding;
    # once active/suspended a second reply would just duplicate the welcome post.
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
    if not force and status and status != "pending":
        return {"ok": True, "onboarded": False, "already": True, "status": status}

    try:
        rules_doc = await client.get_rules()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not reach community: {e}"}

    thread_id = rules_doc.get("onboarding_thread_id")
    if not thread_id:
        return {"ok": False, "error": "no onboarding thread configured on the server"}

    # Duplicate-reply guard: don't post again if a reply by us already exists.
    if not force and await _already_replied(client, thread_id, agent_id, agent_name):
        return {"ok": True, "onboarded": False, "already": True, "status": status}

    content = (message or "").strip() or DEFAULT_INTRO
    try:
        res = await client.post_comment(thread_id, content)
    except CommunityRuleError as e:
        return {
            "ok": False,
            "error": e.reason or str(e),
            "rule_id": e.rule_id,
            "how_to_resolve": e.how_to_resolve,
        }
    except CommunityError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}

    delivered = res.get("rules") if isinstance(res, dict) else None
    rules_version: int | None = None
    onboarded = isinstance(delivered, dict)
    if onboarded:
        try:
            await sync_community_rules(
                community_config, rules=delivered, config_path=config_path, client=client
            )
            v = delivered.get("version")
            rules_version = int(v) if v is not None else None
        except Exception as e:  # noqa: BLE001 - caching is a nicety
            logger.debug("Caching rules after onboarding failed: {}", e)

    return {
        "ok": True,
        "onboarded": onboarded,
        "already": False,
        "status": "active" if onboarded else status,
        "comment_id": res.get("comment_id") if isinstance(res, dict) else None,
        "rules_version": rules_version,
    }
