"""Build a heartbeat-friendly digest of the agent's community next-actions.

The engine's heartbeat loop (#112) folds this into its periodic wake-up so the
agent participates proactively — completing its onboarding connection test,
acknowledging community rules, answering @mentions, voting on relevant
proposals, and reviewing patches — without the user having to ask.

It prefers the personalized task inbox (`GET /agents/tasks`, #16); against an
older cloud without that endpoint it falls back to the public feed. The digest
is plain text appended to the heartbeat decision prompt and never raises, so a
flaky network never breaks the heartbeat. The human's autonomy mode remains the
ceiling for any action the agent ultimately takes.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

# Map each task type to the tool the agent should reach for.
_TASK_TOOL_HINT = {
    "onboarding": "community_comment (reply in the onboarding thread)",
    "rules_ack": "auto-acknowledged",
    "mention": "community_comment",
    "needs_vote": "community_vote",
    "needs_review": "community_review_pr / community_read_feed",
}


def _matches_domains(proposal: dict[str, Any], domains: list[str]) -> bool:
    """Return True when a proposal looks relevant to the agent's domains.

    With no domains configured the agent participates broadly (all relevant).
    """
    if not domains:
        return True
    tags = proposal.get("tags") or []
    haystack = " ".join(
        [
            str(proposal.get("title", "")),
            str(proposal.get("body", "")),
            " ".join(str(t) for t in tags),
        ]
    ).lower()
    return any(domain in haystack for domain in domains)


def _client_for(community_config: Any):
    """Return a connected CommunityClient, or None when not configured."""
    enabled = bool(getattr(community_config, "enabled", False))
    api_base = (getattr(community_config, "api_base", "") or "").strip()
    agent_token = (getattr(community_config, "agent_token", "") or "").strip()
    if not (enabled and api_base and agent_token):
        return None
    from mira_engine.community.client import CommunityClient

    return CommunityClient(api_base, agent_token)


async def gather_community_digest(community_config: Any, limit: int = 10) -> str:
    """Render the agent's actionable community items for the heartbeat.

    Returns an empty string when the community is not connected, there is
    nothing to do, or every request fails.
    """
    client = _client_for(community_config)
    if client is None:
        return ""

    from mira_engine.community.client import CommunityError

    # Preferred path: the personalized task inbox (#16).
    try:
        tasks_resp = await client.get_tasks()
        return await _render_tasks(client, tasks_resp)
    except CommunityError as e:
        logger.debug("Task inbox unavailable, falling back to feed: {}", e)
    except Exception as e:  # noqa: BLE001 - never break the heartbeat
        logger.debug("Task inbox error, falling back to feed: {}", e)

    # Fallback: relevance-filtered public feed (pre-#16 cloud).
    try:
        return await _render_feed(client, community_config, limit)
    except Exception as e:  # noqa: BLE001 - never break the heartbeat
        logger.debug("Heartbeat community digest error: {}", e)
        return ""


async def _render_tasks(client: Any, tasks_resp: dict[str, Any]) -> str:
    """Render the task inbox, auto-acking rules and injecting rules context."""
    tasks = list(tasks_resp.get("tasks", []) or [])

    # Pull the rules summary + onboarding next-step into context, and silently
    # acknowledge the current rules version (an acknowledgement, not an action,
    # and a prerequisite for any participation).
    rules_line = ""
    try:
        rules = await client.get_rules()
        version = rules.get("version")
        if any(t.get("type") == "rules_ack" for t in tasks) and version is not None:
            try:
                await client.ack_rules(int(version))
                logger.info("Acknowledged community rules v{}", version)
                tasks = [t for t in tasks if t.get("type") != "rules_ack"]
            except Exception as e:  # noqa: BLE001
                logger.debug("Rules auto-ack failed: {}", e)
        rule_titles = [str(r.get("title")) for r in (rules.get("rules") or [])][:4]
        if rule_titles:
            rules_line = (
                f"Community rules v{version}: " + "; ".join(rule_titles) + ". "
                "Follow them — high-risk actions still respect your autonomy mode."
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("Rules fetch failed: {}", e)

    if not tasks:
        return ""

    lines = []
    for t in tasks:
        ttype = str(t.get("type", "task"))
        hint = _TASK_TOOL_HINT.get(ttype, "community_read_feed")
        lines.append(
            f"- [{ttype}] {t.get('title')} — {t.get('reason')} "
            f"(do: {hint}; target {t.get('target_id')})"
        )

    header = (
        f"[Mira Community] You have {len(tasks)} actionable item(s) right now. "
        "Address the ones that fit your interests and autonomy mode; skip the rest."
    )
    parts = [header, *lines]
    if rules_line:
        parts.append(rules_line)
    return "\n".join(parts)


async def _render_feed(client: Any, community_config: Any, limit: int) -> str:
    """Legacy fallback: render relevant proposals from the public feed."""
    data = await client.read_feed(limit=limit)
    proposals = data.get("proposals", []) or []
    domains = [str(d).lower() for d in (getattr(community_config, "domains", []) or [])]
    relevant = [p for p in proposals if _matches_domains(p, domains)]
    if not relevant:
        return ""

    lines = [
        f"- [{p.get('status')}] {p.get('title')} "
        f"(score {p.get('score', 0)}, {p.get('comment_count', 0)} comments) "
        f"by {p.get('author_handle')} — id {p.get('id')}"
        for p in relevant
    ]
    header = (
        f"[Mira Community] {len(relevant)} proposal(s) may be relevant to you. "
        "If any aligns with your interests, consider participating with the "
        "community_read_feed / community_comment / community_vote / "
        "community_post_proposal tools. Skip if nothing warrants a response."
    )
    return header + "\n" + "\n".join(lines)
