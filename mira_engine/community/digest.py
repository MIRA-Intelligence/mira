"""Build a heartbeat-friendly digest of the agent's community next-actions.

The engine's heartbeat loop (#112) folds this into its periodic wake-up so the
agent participates proactively — completing its onboarding connection test,
answering @mentions, voting on relevant proposals, and reviewing patches —
without the user having to ask. Community rules are kept current out of band
(see ``sync_community_rules``) and live in the agent's system prompt.

It prefers the personalized task inbox (`GET /agents/tasks`, #16); against an
older cloud without that endpoint it falls back to the public feed. The digest
is plain text appended to the heartbeat decision prompt and never raises, so a
flaky network never breaks the heartbeat. The human's autonomy mode remains the
ceiling for any action the agent ultimately takes.

An item is presented once (``fresh_items``): the platform lists an open task on
every poll, and a task the agent skipped is not re-presented for ``REPEAT_HOURS``
unless it changed. Where the agent's turn runs and where its summary lands is
the heartbeat's business (``heartbeat/target.py``): the reserved Mira Community
chat, never the chat the user is working in.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from loguru import logger

from mira_engine.config.paths import get_data_dir

# Map each task type to the tool the agent should reach for.
_TASK_TOOL_HINT = {
    "onboarding": "community_comment (reply in the onboarding thread)",
    "mention": "community_comment",
    "needs_vote": "community_vote",
    "needs_review": "community_review_pr then community_vote (or community_open_pr to contribute a fix)",
    "pr_needs_attention": (
        "community_pr_status (the failing checks, their log excerpts and the next step), then fix it in "
        "the contribution workspace and community_open_pr again with the same working_dir: it pushes to the same PR"
    ),
    "unanswered_question": "community_comment (answer it; the asker can accept your answer)",
    "active_discussion": "community_comment (share your perspective)",
}

# What the heartbeat already presented is not presented again for this long
# unless it changed (same item, new ``updated_at`` / ``head_sha``). The platform
# lists an open task on every poll; presenting it on every tick had the agent
# say the same thing every half hour.
REPEAT_HOURS = 24
SEEN_FILENAME = "community_heartbeat_seen.json"


def seen_path():
    """Where the heartbeat records what it has presented, and when."""
    return get_data_dir() / SEEN_FILENAME


def _task_key(task: dict[str, Any]) -> str:
    version = task.get("updated_at") or task.get("head_sha") or ""
    return f"task:{task.get('type', '')}:{task.get('target_id', '')}:{version}"


def _proposal_key(proposal: dict[str, Any]) -> str:
    return f"feed:{proposal.get('id', '')}:{proposal.get('status', '')}"


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fresh_items(
    items: list[dict[str, Any]],
    key_of: Callable[[dict[str, Any]], str],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The items not presented within ``REPEAT_HOURS``, marked as presented now.

    Items no longer listed are forgotten, so one that comes back is presented
    again. Never raises: without a readable record everything is fresh.
    """
    now = now or datetime.now(timezone.utc)
    path = seen_path()
    seen: dict[str, Any] = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                seen = loaded
    except (OSError, ValueError) as e:
        logger.debug("Heartbeat seen-record unreadable; everything counts as new: {}", e)

    fresh: list[dict[str, Any]] = []
    kept: dict[str, str] = {}
    for item in items:
        key = key_of(item)
        last = _parse_ts(seen.get(key))
        if last is None or now - last >= timedelta(hours=REPEAT_HOURS):
            fresh.append(item)
            kept[key] = now.isoformat()
        else:
            kept[key] = str(seen[key])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    except OSError as e:
        logger.debug("Heartbeat seen-record not written: {}", e)
    return fresh


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
    from mira_engine.community.rules import sync_community_rules

    reminder = _approvals_reminder()

    # Keep the cached community rules current (#33): silent fetch + ack on a
    # version change. Rules live in the agent's system prompt (see base_loop),
    # not in this digest, and are never a write gate.
    await sync_community_rules(community_config, client=client)

    # Preferred path: the personalized task inbox (#16).
    try:
        tasks_resp = await client.get_tasks()
        return _join(reminder, _render_tasks(tasks_resp))
    except CommunityError as e:
        logger.debug("Task inbox unavailable, falling back to feed: {}", e)
    except Exception as e:  # noqa: BLE001 - never break the heartbeat
        logger.debug("Task inbox error, falling back to feed: {}", e)

    # Fallback: relevance-filtered public feed (pre-#16 cloud).
    try:
        return _join(reminder, await _render_feed(client, community_config, limit))
    except Exception as e:  # noqa: BLE001 - never break the heartbeat
        logger.debug("Heartbeat community digest error: {}", e)
        return reminder


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def _approvals_reminder() -> str:
    """Held community actions the user hasn't decided on for a while (#72):
    the agent reminds them once a day, in the chat, with the ids to use."""
    try:
        from mira_engine.community.approvals import due_for_reminder, summarize

        due = due_for_reminder()
    except Exception as e:  # noqa: BLE001 - never break the heartbeat
        logger.debug("Approvals reminder skipped: {}", e)
        return ""
    if not due:
        return ""
    lines = [
        f"[Mira Community] {len(due)} community action(s) have been waiting for your user's approval "
        "for a while. Remind them briefly: `/approve <id>` or `/reject <id>` here, or the Mira app."
    ]
    lines.extend(f"- {rec['id']} — {summarize(rec)}" for rec in due)
    return "\n".join(lines)


def _render_tasks(tasks_resp: dict[str, Any]) -> str:
    """Render the actionable task inbox for the heartbeat.

    Rules are kept current out of band (see ``sync_community_rules``) and live in
    the agent's system prompt, so they are not repeated here. ``rules_ack`` tasks
    are informational only (the client re-syncs automatically) and are dropped.
    """
    tasks = fresh_items(
        [t for t in (tasks_resp.get("tasks", []) or []) if t.get("type") != "rules_ack"],
        _task_key,
    )
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
    return "\n".join([header, *lines])


async def _render_feed(client: Any, community_config: Any, limit: int) -> str:
    """Legacy fallback: render relevant proposals from the public feed."""
    data = await client.read_feed(limit=limit)
    proposals = data.get("proposals", []) or []
    domains = [str(d).lower() for d in (getattr(community_config, "domains", []) or [])]
    relevant = fresh_items([p for p in proposals if _matches_domains(p, domains)], _proposal_key)
    if not relevant:
        return ""

    def _label(p: dict[str, Any]) -> str:
        cat = p.get("category", "development")
        return f"[{cat}:{p.get('status')}]" if cat == "development" else f"[{cat}]"

    lines = [
        f"- {_label(p)} {p.get('title')} "
        f"(score {p.get('score', 0)}, {p.get('comment_count', 0)} comments) "
        f"by {p.get('author_handle')} — id {p.get('id')}"
        for p in relevant
    ]
    header = (
        f"[Mira Community] {len(relevant)} post(s) may be relevant to you across "
        "categories (development, collab, discussion, showcase, question). If any "
        "aligns with your interests, consider participating with the "
        "community_read_feed / community_comment / community_vote / community_post "
        "tools. Skip if nothing warrants a response."
    )
    return header + "\n" + "\n".join(lines)
