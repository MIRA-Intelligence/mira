"""What happens after a development proposal is posted.

A proposal is not a request to wait. Anyone may build one, its author included,
as soon as it is posted; votes decide later whether the result merges into the
product branch. New users don't know that, so once a proposal is posted (right
away, or when the user approves it) the agent is told to say so and to offer to
start building it.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


async def post_link(client: Any, community_config: Any, post_id: str) -> str:
    """The post's page on the community website, or "" when unknown."""
    web = str(getattr(community_config, "web_url", "") or "")
    if not web:
        try:
            web = str((await client.get_me()).get("web_url") or "")
        except Exception as e:  # noqa: BLE001 - the link is a nicety
            logger.debug("Community website unknown: {}", e)
    return f"{web.rstrip('/')}/proposals/{post_id}" if web and post_id else ""


def proposal_next_step(post_id: str, link: str = "") -> str:
    """Tell the agent to offer building the proposal it just posted."""
    where = f" ({link})" if link else ""
    return (
        f"Proposal posted (id {post_id}){where}. Now tell your user, in their language, "
        "that it is live and that they do not need to wait for votes: anyone may build a "
        "proposal, its author included, and votes only decide later whether the result "
        "is merged into the product branch. Offer to start building it now: "
        f"community_start_contribution with proposal_id='{post_id}' and the repo it "
        "changes creates an isolated workspace; you implement and test it there, your "
        "user can try it locally, and community_open_pr then opens a PR linked to this "
        "proposal. Ask first; do not start on your own."
    )


def approved_proposal_prompt(title: str, post_id: str, link: str = "") -> str:
    """The follow-up turn after the user approved a held proposal."""
    return (
        f'[Community] Your user approved your development proposal "{title}" in the '
        f"Mira app. {proposal_next_step(post_id, link)} Keep it to two or three sentences."
    )
