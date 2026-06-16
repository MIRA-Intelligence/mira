"""Build a heartbeat-friendly digest of relevant community activity.

The engine's heartbeat loop (#112) uses this to fold recent community proposals
into its periodic wake-up so the agent can participate "seamlessly" — comment on
or vote for items that match the user's configured ``domains`` — without the user
having to ask. The digest is plain text that gets appended to the heartbeat
decision prompt; it never raises, so a flaky network never breaks the heartbeat.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


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


async def gather_community_digest(community_config: Any, limit: int = 10) -> str:
    """Fetch recent proposals and render those relevant to the agent.

    Returns an empty string when the community is not connected, the feed is
    empty, nothing matches the configured domains, or the request fails.
    """
    enabled = bool(getattr(community_config, "enabled", False))
    api_base = (getattr(community_config, "api_base", "") or "").strip()
    agent_token = (getattr(community_config, "agent_token", "") or "").strip()
    if not (enabled and api_base and agent_token):
        return ""

    from mira_engine.community.client import CommunityClient, CommunityError

    client = CommunityClient(api_base, agent_token)
    try:
        data = await client.read_feed(limit=limit)
    except CommunityError as e:
        logger.debug("Heartbeat community digest skipped: {}", e)
        return ""
    except Exception as e:  # noqa: BLE001 - never break the heartbeat
        logger.debug("Heartbeat community digest error: {}", e)
        return ""

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
