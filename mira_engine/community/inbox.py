"""The user's community inbox: notifications the user chose to receive.

The agent sees every community event and decides what to do with it on its
own. The *user* only hears about the kinds they picked in
``community.notifications``, like mail filters:

* ``reply``    someone answered their agent's post or comment
* ``mention``  someone @mentioned their agent
* ``review``   their agent was asked to review a pull request
* ``work``     their work orders and pull requests changed state
* ``activity`` everything else: votes wanted, open questions, new work
               orders, level-ups and badges

An empty list means no notifications at all: the agent runs fully on its own.
Actions held for approval are not notifications; they stay in the approval
queue (``approvals.py``) whatever is chosen here, because they need a decision.

Stored locally in ``<data dir>/community_inbox.jsonl`` (never uploaded),
newest ``MAX_ITEMS`` kept, de-duplicated per (event type, target).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from mira_engine.config.paths import get_data_dir
from mira_engine.utils.locks import locked_update_text

INBOX_FILENAME = "community_inbox.jsonl"
MAX_ITEMS = 500
NOTIFICATION_KINDS = ("reply", "mention", "review", "work", "activity")
DEFAULT_NOTIFICATIONS = ["reply", "mention", "review", "work"]

_KIND_BY_TYPE = {
    "reply": "reply",
    "mention": "mention",
    "needs_review": "review",
    "work_order_lease": "work",
    "work_order_cancelled": "work",
    "pr_registered": "work",
    "pr_policy": "work",
    "pr_reviewed": "work",
    "pr_rejected": "work",
    "pr_merged": "work",
    "pr_landed": "work",
    "pr_reverted": "work",
    "needs_vote": "activity",
    "unanswered_question": "activity",
    "active_discussion": "activity",
    "work_order_available": "activity",
    "summarize_thread": "activity",
    "level_up": "activity",
    "badge_earned": "activity",
}

_LEASE_TITLES = {
    "accepted": "Your work order contribution was accepted",
    "expired": "Your work order lease expired",
    "completed_by_other": "A work order you held was completed by someone else",
    "reopened": "A work order you held is open again",
    "released": "A work order lease was released",
}

_PR_TITLES = {
    "pr_registered": "Your pull request was registered",
    "pr_policy": "Your pull request breaks a contribution boundary",
    "pr_reviewed": "Your pull request was reviewed",
    "pr_rejected": "Your pull request was rejected",
    "pr_merged": "Your pull request was merged",
    "pr_landed": "Your pull request landed",
    "pr_reverted": "Your pull request was reverted after landing",
}


def inbox_path():
    return get_data_dir() / INBOX_FILENAME


def kind_of(event: dict[str, Any]) -> str | None:
    """The notification kind of a community event, or None if it is not one."""
    return _KIND_BY_TYPE.get(str(event.get("type") or ""))


def normalize_kinds(kinds: Any) -> list[str]:
    """Validate a user's choice; raises ValueError on unknown kinds."""
    if not isinstance(kinds, (list, tuple)):
        raise ValueError("notifications must be a list")
    unknown = [k for k in kinds if k not in NOTIFICATION_KINDS]
    if unknown:
        raise ValueError(f"unknown notification kinds: {', '.join(map(str, unknown))}")
    return [k for k in NOTIFICATION_KINDS if k in kinds]


def _title(event: dict[str, Any]) -> str:
    etype = str(event.get("type") or "")
    if event.get("title"):
        return str(event["title"])
    if etype == "work_order_lease":
        state = str(event.get("state") or "")
        return _LEASE_TITLES.get(state, f"Work order update: {state.replace('_', ' ') or 'changed'}")
    if etype == "work_order_cancelled":
        return "A work order you held was cancelled"
    if etype in _PR_TITLES:
        verdict = event.get("verdict")
        return _PR_TITLES[etype] + (f" ({verdict})" if verdict else "")
    if etype == "level_up":
        return f"Your account reached {event.get('label') or 'a new level'} {event.get('name') or ''}".strip()
    if etype == "badge_earned":
        return f"New badge: {event.get('name') or event.get('badge_id') or 'badge'}"
    return etype.replace("_", " ").capitalize() or "Community update"


def _body(event: dict[str, Any]) -> str:
    for key in ("content", "body", "reason", "description"):
        if event.get(key):
            return str(event[key])
    if event.get("problems"):
        return "\n".join(str(p) for p in event["problems"])
    return ""


def _thread_id(event: dict[str, Any]) -> str:
    for key in ("thread_id", "proposal_id"):
        if event.get(key):
            return str(event[key])
    body = ((event.get("action") or {}).get("body") or {}) if isinstance(event.get("action"), dict) else {}
    return str(body.get("thread_id") or body.get("proposal_id") or "")


def _key(event: dict[str, Any]) -> str:
    target = (
        event.get("target_id")
        or event.get("pr_id")
        or event.get("work_order_id")
        or event.get("badge_id")
        or event.get("level")
        or event.get("id")
        or uuid.uuid4().hex
    )
    suffix = f":{event.get('state')}" if event.get("state") else ""
    return f"{event.get('type')}:{target}{suffix}"


def to_item(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = kind_of(event)
    if kind is None:
        return None
    now = datetime.now(timezone.utc)
    return {
        "id": f"{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:8]}",
        "key": _key(event),
        "kind": kind,
        "type": str(event.get("type")),
        "title": _title(event),
        "body": _body(event),
        "from": str(event.get("author") or event.get("actor") or ""),
        "thread_id": _thread_id(event),
        "url": str(event.get("url") or ""),
        # Structured details so a client can word the title in its own language.
        "subject": str(event.get("proposal_title") or ""),
        "on": str(event.get("on") or ""),
        "state": str(event.get("state") or ""),
        "verdict": str(event.get("verdict") or ""),
        "created_at": now.isoformat(),
        "read": False,
    }


def _parse(text: str) -> list[dict[str, Any]]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _dump(items: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items[-MAX_ITEMS:])


def record_event(event: dict[str, Any], enabled_kinds: list[str] | tuple[str, ...]) -> dict[str, Any] | None:
    """File ``event`` in the inbox if the user receives its kind. Never raises."""
    item = to_item(event)
    if item is None:
        return None
    if item["kind"] not in enabled_kinds:
        # A reply that also @mentions the user is sent once, as a reply flagged
        # ``mentioned``. Someone who follows mentions but not replies gets it,
        # filed under mentions so it shows in the folder they follow.
        if not (event.get("mentioned") and "mention" in enabled_kinds):
            return None
        item["kind"] = "mention"
    added: dict[str, Any] = {}

    def update(text: str) -> str:
        items = _parse(text)
        if any(i.get("key") == item["key"] for i in items):
            return text
        added.update(item)
        return _dump([*items, item])

    try:
        path = inbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_update_text(path, update)
    except Exception as e:  # noqa: BLE001 - the inbox must never break the channel
        logger.warning("Failed to record community notification: {}", e)
        return None
    return added or None


def list_items(kind: str | None = None, unread_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    """Newest first."""
    try:
        items = _parse(inbox_path().read_text(encoding="utf-8"))
    except OSError:
        return []
    if kind:
        items = [i for i in items if i.get("kind") == kind]
    if unread_only:
        items = [i for i in items if not i.get("read")]
    return list(reversed(items))[:limit]


def unread_counts() -> dict[str, Any]:
    by_kind = {k: 0 for k in NOTIFICATION_KINDS}
    for item in list_items(unread_only=True, limit=MAX_ITEMS):
        if item.get("kind") in by_kind:
            by_kind[item["kind"]] += 1
    return {"total": sum(by_kind.values()), "by_kind": by_kind}


def mark_read(ids: list[str] | None = None, *, kind: str | None = None, everything: bool = False) -> int:
    """Mark items read: the given ids, a whole kind, or everything. Returns how many changed."""
    wanted = set(ids or [])
    changed = 0

    def update(text: str) -> str:
        nonlocal changed
        items = _parse(text)
        for item in items:
            if item.get("read"):
                continue
            if everything or item.get("id") in wanted or (kind and item.get("kind") == kind):
                item["read"] = True
                changed += 1
        return _dump(items)

    path = inbox_path()
    if not path.exists():
        return 0
    locked_update_text(path, update)
    return changed
