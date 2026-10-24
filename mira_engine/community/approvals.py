"""Persistent store for HITL/hybrid community approvals.

When the :class:`~mira_engine.community.autonomy.AutonomyGate` holds a write
action, it is appended to ``~/.mira/community_approvals.jsonl``. The desktop
app's approval inbox (#114) reads that queue over ``/api/community/approvals``
and decides each item; on approval the action is executed against the cloud via
:class:`~mira_engine.community.client.CommunityClient`.

This module is the single source of truth for the on-disk format so the writer
(the gate) and the readers/deciders (the UI gateway) never drift.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from mira_engine.config.paths import get_data_dir

ApprovalStatus = str  # "pending" | "approved" | "rejected" | "failed" | "expired"
STATUSES = ("pending", "approved", "rejected", "failed", "expired")
# A held action nobody decides on is dropped after this long (#72): the agent
# is told to ask again rather than a stale proposal going out weeks later.
APPROVAL_TTL_HOURS = 7 * 24
# The heartbeat reminds the user of pending approvals older than this, at most
# once per REMIND_EVERY_HOURS per item.
REMIND_AFTER_HOURS = 6
REMIND_EVERY_HOURS = 24

# Actions whose approval the agent follows up in the conversation it asked from.
_FOLLOWED_UP = ("post_proposal", "claim_proposal", "open_pr", "update_pr")


def approvals_path():
    """Location of the append-only approvals log."""
    return get_data_dir() / "community_approvals.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _age_hours(record: dict[str, Any], now: datetime) -> float:
    created = _parse(record.get("created_at"))
    if created is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 3600


def summarize(record: dict[str, Any]) -> str:
    """One line a human can decide on: the action and what it is about."""
    action = str(record.get("action") or "?")
    payload = record.get("payload") or {}
    about = (
        payload.get("title")
        or payload.get("content")
        or payload.get("body")
        or payload.get("proposal_id")
        or payload.get("work_order_id")
        or payload.get("pr_id")
        or ""
    )
    about = " ".join(str(about).split())
    if len(about) > 80:
        about = about[:77] + "…"
    return f"{action}" + (f": {about}" if about else "")


def append_approval(
    action: str, payload: dict[str, Any], origin: dict[str, str] | None = None
) -> str:
    """Persist a pending action for later human review. Returns its id.

    The id pairs a millisecond timestamp (so ids sort roughly chronologically)
    with a short random suffix so rapid successive calls never collide.
    ``origin`` (``channel``/``chat_id``/``project_id``) is the conversation the
    agent asked from, so the outcome can be followed up there.
    """
    approval_id = f"{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
    record = {
        "id": approval_id,
        "action": action,
        "payload": payload,
        "status": "pending",
        "created_at": _now(),
    }
    if origin:
        record["origin"] = origin
    path = approvals_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("Failed to write community approval: {}", e)
    return approval_id


def _read_all() -> list[dict[str, Any]]:
    path = approvals_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("id"):
                    records.append(obj)
    except OSError as e:
        logger.warning("Failed to read community approvals: {}", e)
    return records


def _write_all(records: list[dict[str, Any]]) -> None:
    path = approvals_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except OSError as e:
        logger.warning("Failed to persist community approvals: {}", e)


def _latest() -> dict[str, dict[str, Any]]:
    """Latest record per id, in file order."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in _read_all():
        latest[str(rec["id"])] = rec
    return latest


def expire_stale(now: datetime | None = None) -> list[str]:
    """Mark pending approvals older than ``APPROVAL_TTL_HOURS`` as expired.
    Returns the ids that just expired."""
    now = now or datetime.now(timezone.utc)
    latest = _latest()
    expired = [
        rid for rid, rec in latest.items()
        if rec.get("status") == "pending" and _age_hours(rec, now) >= APPROVAL_TTL_HOURS
    ]
    if expired:
        for rid in expired:
            latest[rid]["status"] = "expired"
            latest[rid]["decided_at"] = now.isoformat()
        _write_all(list(latest.values()))
    return expired


def list_approvals(status: str | None = None) -> list[dict[str, Any]]:
    """Return stored approvals, newest first, optionally filtered by status.

    The last record for a given id wins, so a decision recorded as a later line
    supersedes the original pending entry even before compaction. Pending
    items past their TTL are expired first.
    """
    expire_stale()
    records = list(_latest().values())
    if status:
        records = [r for r in records if r.get("status") == status]
    records.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return records


def due_for_reminder(now: datetime | None = None) -> list[dict[str, Any]]:
    """Pending approvals old enough to remind the user about, not reminded
    recently. Marks them reminded."""
    now = now or datetime.now(timezone.utc)
    latest = _latest()
    due: list[dict[str, Any]] = []
    for rec in latest.values():
        if rec.get("status") != "pending" or _age_hours(rec, now) < REMIND_AFTER_HOURS:
            continue
        last = _parse(rec.get("reminded_at"))
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is not None and (now - last).total_seconds() / 3600 < REMIND_EVERY_HOURS:
            continue
        rec["reminded_at"] = now.isoformat()
        due.append(rec)
    if due:
        _write_all(list(latest.values()))
    return due


def get_approval(approval_id: str) -> dict[str, Any] | None:
    for rec in list_approvals():
        if str(rec.get("id")) == str(approval_id):
            return rec
    return None


def set_status(approval_id: str, status: str, detail: str | None = None) -> dict[str, Any] | None:
    """Mark an approval approved/rejected/failed. Returns the updated record."""
    seen = _latest()
    target = seen.get(str(approval_id))
    if target is None:
        return None
    target["status"] = status
    target["decided_at"] = _now()
    if detail is not None:
        target["detail"] = detail
    _write_all(list(seen.values()))
    return target


async def follow_up_prompt(record: dict[str, Any], result: dict[str, Any], community_config: Any) -> str | None:
    """What the agent is told back in the conversation after the user decided
    on a held action: a posted proposal is offered for building, a claimed one
    is built, an opened PR is reported — and a failed execution is handed back
    so the agent can fix it (a privacy-filter hit, a git problem) and ask again.
    None when there is nothing to follow up."""
    from mira_engine.community.proposals import (
        approved_claim_prompt,
        approved_proposal_prompt,
        post_link,
    )

    action = str(record.get("action") or "")
    payload = record.get("payload") or {}
    title = str(payload.get("title") or "")
    if not result.get("ok"):
        if action not in _FOLLOWED_UP:
            return None
        return (
            f"[Community] Your user approved the community action '{summarize(record)}', but it could not "
            f"be carried out: {result.get('detail') or 'unknown error'}. Fix the cause (for a privacy-filter "
            "hit, rewrite the text without the flagged words) and submit it again, or tell your user why not."
        )
    if action == "post_proposal" and payload.get("category", "development") == "development":
        post_id = str(result.get("post_id") or "")
        if not post_id:
            return None
        from mira_engine.community.client import CommunityClient

        client = CommunityClient(
            getattr(community_config, "api_base", "") or "", getattr(community_config, "agent_token", "") or ""
        )
        return approved_proposal_prompt(title, post_id, await post_link(client, community_config, post_id))
    if action == "claim_proposal" and payload.get("proposal_id"):
        return approved_claim_prompt(title, str(payload["proposal_id"]))
    if action == "open_pr":
        return (
            f"[Community] Your user approved opening the pull request \"{title}\". {result.get('detail', '')} "
            "Tell your user briefly, with the link; you will hear here when it is reviewed, merged or reverted."
        )
    if action == "update_pr":
        return (
            f"[Community] Your user approved pushing the follow-up to the pull request \"{title}\". "
            f"{result.get('detail', '')} Tell your user briefly; you will hear here when CI and the "
            "reviewers have looked at the new head."
        )
    return None


async def execute_approval(record: dict[str, Any], community_config: Any) -> dict[str, Any]:
    """Run an approved community action against the cloud.

    Returns a small result dict ``{"ok": bool, "detail": str}``. Raises nothing;
    failures are reported in the result so the caller can surface them.
    """
    from mira_engine.community.client import CommunityClient, CommunityError

    if record.get("action") == "persona_update":
        # Local only (mira#212): the user approved the drafted community persona.
        from pathlib import Path

        from mira_engine.community.persona import apply_persona

        payload = record.get("payload") or {}
        try:
            path = apply_persona(Path(payload["workspace"]), str(payload["draft"]))
        except (KeyError, OSError) as e:
            return {"ok": False, "detail": f"could not write the community persona: {e}"}
        return {"ok": True, "detail": f"community persona updated ({path})"}

    api_base = getattr(community_config, "api_base", "") or ""
    agent_token = getattr(community_config, "agent_token", "") or ""
    if not api_base or not agent_token:
        return {"ok": False, "detail": "not connected to the community (run login)"}

    action = record.get("action")
    payload = record.get("payload") or {}
    origin = record.get("origin") or None
    client = CommunityClient(api_base, agent_token)
    try:
        if action in ("post_proposal", "post"):
            res = await client.create_proposal(
                payload.get("title", ""),
                payload.get("body", ""),
                category=payload.get("category", "development"),
                tags=payload.get("tags"),
            )
            post_id = str(res.get("id") or "")
            if post_id and payload.get("category", "development") == "development":
                from mira_engine.community.origins import remember

                remember("proposal", post_id, origin, title=payload.get("title", ""))
            return {"ok": True, "detail": f"post created ({res.get('id', '?')})", "post_id": res.get("id")}
        if action in ("open_pr", "update_pr"):
            from mira_engine.community.contribute import (
                ContributeError,
                open_and_register_pr,
                pr_summary,
            )
            from mira_engine.community.origins import remember, update
            from mira_engine.community.privacy import default_guard

            try:
                out = await open_and_register_pr(
                    client,
                    working_dir=str(payload.get("working_dir") or ""),
                    title=str(payload.get("title") or ""),
                    body=str(payload.get("body") or ""),
                    proposal_id=payload.get("proposal_id") or None,
                    guard=default_guard(),
                )
            except ContributeError as e:
                return {"ok": False, "detail": f"could not open the PR: {e}"}
            if out.get("pr_id"):
                remember(
                    "pr", out["pr_id"], origin,
                    url=out["url"], plugin_ids=out["plugin_ids"], working_dir=payload.get("working_dir", ""),
                )
            elif out.get("updated") and payload.get("pr_id"):
                # A human looked at this PR: the follow-up pushes start counting afresh.
                update("pr", str(payload["pr_id"]), fix_rounds=0)
            return {"ok": True, "detail": pr_summary(out), "pr_url": out["url"], "pr_id": out.get("pr_id")}
        if action == "accept_answer":
            res = await client.accept_answer(
                payload.get("post_id", ""), payload.get("comment_id", "")
            )
            return {
                "ok": True,
                "detail": f"answer accepted ({res.get('accepted_comment_id', '?')})",
            }
        if action == "comment":
            res = await client.post_comment(
                payload.get("thread_id", ""),
                payload.get("content", ""),
                payload.get("reply_to"),
            )
            return {"ok": True, "detail": f"comment posted ({res.get('comment_id', '?')})"}
        if action == "vote":
            res = await client.vote(payload.get("proposal_id", ""), int(payload.get("value", 1)))
            return {"ok": True, "detail": f"vote cast ({res.get('score', '?')})"}
        if action == "claim_work_order":
            res = await client.claim_work_order(payload.get("work_order_id", ""))
            claim = res.get("claim") or {}
            return {"ok": True, "detail": f"work order claimed (lease until {claim.get('expires_at', '?')})"}
        if action in ("claim_proposal", "renew_proposal_claim"):
            res = await client.claim_proposal(payload.get("proposal_id", ""))
            claim = res.get("claim") or {}
            return {"ok": True, "detail": f"proposal claimed (until {claim.get('claim_expires_at') or 'its PR closes'})"}
        if action == "release_proposal_claim":
            await client.release_proposal(payload.get("proposal_id", ""))
            return {"ok": True, "detail": "proposal claim released"}
        if action == "review":
            res = await client.review_pr(
                payload.get("pr_id", ""),
                payload.get("verdict", ""),
                payload.get("head_sha", ""),
                payload.get("body", ""),
            )
            return {"ok": True, "detail": f"review recorded (score {res.get('review_score', '?')})"}
        if action == "submit_patch":
            res = await client.submit_patch(
                payload.get("proposal_id", ""),
                payload.get("repo", ""),
                payload.get("diff", ""),
                payload.get("title", ""),
                payload.get("body", ""),
                payload.get("base_ref", "main"),
            )
            return {"ok": True, "detail": f"patch submitted ({res.get('id', '?')})"}
        return {"ok": False, "detail": f"unsupported action '{action}'"}
    except CommunityError as e:
        return {"ok": False, "detail": str(e)}
    except Exception as e:  # noqa: BLE001 - never let execution crash the gateway
        logger.warning("Failed to execute community approval {}: {}", record.get("id"), e)
        return {"ok": False, "detail": f"execution error: {e}"}
