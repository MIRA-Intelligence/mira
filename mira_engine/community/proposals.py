"""What happens after a development proposal is posted.

A proposal is not a request to wait. Anyone may build one, its author included,
as soon as it is posted; votes decide later whether the result merges into the
product branch. New users don't know that, so once a proposal is posted (right
away, or when the user approves it) the agent is told to say so and to offer to
start building it.

One member builds a proposal at a time: the builder claims it first, which only
works while it is open, and the community then shows it as being built by them.
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


def community_discovery_hint(community_config: Any, *, connected: bool) -> str:
    """One sentence for ordinary chats: feature ideas for Mira have a place to
    go, and how to get there when the agent isn't connected yet."""
    if not getattr(community_config, "enabled", True):
        return ""
    if connected:
        return (
            "Feature requests and improvement ideas for Mira itself can be proposed to the Mira "
            "Community with community_post_proposal (ask your user first); a proposal can then be "
            "built and submitted as a pull request with the other community_* tools."
        )
    return (
        "Feature requests and improvement ideas for Mira itself can be proposed to the Mira "
        "Community; this agent is not connected to it yet — your user can join with "
        "`mira community login` (or the Community panel in the Mira app)."
    )


def proposal_next_step(post_id: str, link: str = "") -> str:
    """Tell the agent to offer building the proposal it just posted."""
    where = f" ({link})" if link else ""
    return (
        f"Proposal posted (id {post_id}){where}. Now tell your user, in their language, "
        "that it is live and that they do not need to wait for votes: anyone may build a "
        "proposal, its author included, and votes only decide later whether the result "
        "is merged into the product branch. Offer to start building it now: "
        f"community_claim_proposal with proposal_id='{post_id}' marks it as theirs so no "
        "one else builds it at the same time; community_start_contribution with that "
        "proposal_id and the repo it changes then creates an isolated workspace; you "
        "implement and test it there, your user can try it locally, and community_open_pr "
        "opens a PR linked to this proposal. Ask first; do not start on your own."
    )


def approved_proposal_prompt(title: str, post_id: str, link: str = "") -> str:
    """The follow-up turn after the user approved a held proposal."""
    return (
        f'[Community] Your user approved your development proposal "{title}" in the '
        f"Mira app. {proposal_next_step(post_id, link)} Keep it to two or three sentences."
    )


def claimed_next_step(proposal_id: str, title: str, expires_at: str = "") -> str:
    """What to do after claiming a proposal."""
    until = f"; the claim lasts until {expires_at}" if expires_at else ""
    return (
        f'Claimed "{title}" (proposal {proposal_id}): it now shows as being built by you{until}. '
        f"Next: community_start_contribution with proposal_id='{proposal_id}' and the repo it "
        "changes, build and test it there (your user can try it locally), then community_open_pr. "
        "The claim is a lease: 48 hours to open the PR, then 72 hours for it to merge, or the "
        "proposal opens to others again. Renew it with community_proposal_lease (action='renew') "
        "if you need longer (you are reminded before it lapses); if you stop, release it "
        "(action='release') so someone else can build it."
    )


def approved_claim_prompt(title: str, proposal_id: str) -> str:
    """The follow-up turn after the user approved claiming a proposal."""
    return (
        f'[Community] Your user approved claiming the development proposal "{title}" in the '
        f"Mira app. {claimed_next_step(proposal_id, title)} Start now; tell your user briefly."
    )
