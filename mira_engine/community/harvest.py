"""Weekly community harvest report (design §10.4, mira#214).

What the agent did in the community for its user this week, and what it
brought back: its contributions and their status, the discussions and
questions that match the user's interests, what is waiting on it, and what
the community cost. The report is built **locally** and stays local:

* it only *reads* from the platform (GET requests) — nothing is posted;
* relevance is judged against the user's private interests (``USER.md``)
  on this machine, which is why that file may be used here and nowhere else
  on the community path;
* it spends no model tokens.

Written to ``<workspace>/community/reports/<year>-W<week>.md``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.community.budget import CommunityBudget

REPORT_DIR = Path("community") / "reports"
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "about", "your", "their", "using", "how"}


def report_path(workspace: Path, today: date | None = None) -> Path:
    y, w, _ = (today or date.today()).isocalendar()
    return workspace / REPORT_DIR / f"{y}-W{w:02d}.md"


def last_report_date(workspace: Path) -> date | None:
    reports = sorted((workspace / REPORT_DIR).glob("*.md"))
    if not reports:
        return None
    return datetime.fromtimestamp(reports[-1].stat().st_mtime).date()


def is_due(workspace: Path, every_days: int, today: date | None = None) -> bool:
    if every_days <= 0:
        return False
    last = last_report_date(workspace)
    return last is None or ((today or date.today()) - last).days >= every_days


def interests(workspace: Path, domains: list[str] | None = None) -> set[str]:
    """Keywords for the user's interests: USER.md topics + public domains."""
    words: set[str] = {d.lower() for d in domains or []}
    for name in ("USER.md", "USER.local.md"):
        try:
            text = (workspace / name).read_text(encoding="utf-8")
        except OSError:
            continue
        in_topics = False
        for line in text.splitlines():
            if line.startswith("#"):
                in_topics = "interest" in line.lower() or "topic" in line.lower()
                continue
            if in_topics and line.strip().startswith(("-", "*")):
                words |= {w for w in _WORD.findall(line.lower()) if w not in _STOP}
    return words


def _relevant(post: dict[str, Any], keywords: set[str]) -> bool:
    text = " ".join([str(post.get("title") or ""), " ".join(post.get("tags") or [])]).lower()
    return any(k in text for k in keywords)


def _since(iso: str | None, days: int) -> bool:
    if not iso:
        return False
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    return ts >= datetime.now(timezone.utc) - timedelta(days=days)


async def build_report(client: Any, workspace: Path, community_config: Any, budget: CommunityBudget, days: int = 7) -> str:
    """Assemble the report from read-only platform calls + local data."""

    async def safe(coro, default):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001 - a partial report beats none
            logger.debug("harvest: {}", e)
            return default

    me = await safe(client.get_me(), {})
    prs = (await safe(client.list_prs(), {})).get("prs") or []
    work = await safe(client.list_work_orders(), {})
    tasks = (await safe(client.get_tasks(), {})).get("tasks") or []
    feed = (await safe(client.read_feed(limit=50), {})).get("proposals") or []
    keywords = interests(workspace, list(getattr(community_config, "domains", None) or []))

    today = date.today()
    lines = [f"# Community report — week of {today - timedelta(days=days - 1)} to {today}", ""]

    lines += ["## You and the community", ""]
    if me:
        lines.append(f"- Level {me.get('level_label', '?')} ({me.get('level_name', '')}), reputation {me.get('reputation', '?')}")
        badges = [b.get("badge_id") for b in me.get("badges") or [] if isinstance(b, dict)]
        if badges:
            lines.append(f"- Badges: {', '.join(badges)}")
    week_prs = [p for p in prs if _since(p.get("created_at"), days) or _since(p.get("merged_at"), days)]
    for p in week_prs:
        lines.append(f"- PR {p.get('repo')}#{p.get('pr_number')}: **{p.get('state')}** ({p.get('url')})")
    for c in work.get("claims") or []:
        wo = c.get("work_order") or {}
        lines.append(f"- Work order \"{wo.get('title')}\": {c.get('status')}, lease until {c.get('expires_at')}")
    if len(lines) == 4:
        lines.append("- No contributions this week.")

    relevant = [p for p in feed if _relevant(p, keywords)] if keywords else []
    lines += ["", "## Worth a look for you", ""]
    if relevant:
        for p in relevant[:10]:
            lines.append(f"- [{p.get('category', 'post')}] {p.get('title')} — score {p.get('score', 0)}, {p.get('comment_count', 0)} comments (id {p.get('id')})")
    else:
        lines.append("- Nothing matched your interests this week." if keywords else "- Add interests to your profile to get matches here.")

    waiting = [t for t in tasks if t.get("type") in ("mention", "needs_review", "work_order_lease", "unanswered_question")]
    lines += ["", "## Waiting on your agent", ""]
    lines += [f"- {t.get('title')}" for t in waiting[:10]] or ["- Nothing pending."]

    window = [d for d in budget.days() if (today - date.fromisoformat(d.day)).days < days]
    tokens = sum(d.tokens for d in window)
    lines += [
        "",
        "## What it cost",
        "",
        f"- {sum(d.turns for d in window)} community turns, {tokens:,} tokens"
        + (f" (daily budget {budget.limit:,})" if budget.limit else ""),
        f"- {sum(d.skipped for d in window)} events skipped by the budget/triage",
    ]
    return "\n".join(lines) + "\n"


async def write_report(client: Any, workspace: Path, community_config: Any, budget: CommunityBudget) -> Path:
    path = report_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(await build_report(client, workspace, community_config, budget), encoding="utf-8")
    return path
