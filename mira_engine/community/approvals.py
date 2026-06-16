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

ApprovalStatus = str  # "pending" | "approved" | "rejected"


def approvals_path():
    """Location of the append-only approvals log."""
    return get_data_dir() / "community_approvals.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_approval(action: str, payload: dict[str, Any]) -> str:
    """Persist a pending action for later human review. Returns its id.

    The id pairs a millisecond timestamp (so ids sort roughly chronologically)
    with a short random suffix so rapid successive calls never collide.
    """
    approval_id = f"{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
    record = {
        "id": approval_id,
        "action": action,
        "payload": payload,
        "status": "pending",
        "created_at": _now(),
    }
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


def list_approvals(status: str | None = None) -> list[dict[str, Any]]:
    """Return stored approvals, newest first, optionally filtered by status.

    The last record for a given id wins, so a decision recorded as a later line
    supersedes the original pending entry even before compaction.
    """
    latest: dict[str, dict[str, Any]] = {}
    for rec in _read_all():
        latest[str(rec["id"])] = rec
    records = list(latest.values())
    if status:
        records = [r for r in records if r.get("status") == status]
    records.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return records


def get_approval(approval_id: str) -> dict[str, Any] | None:
    for rec in list_approvals():
        if str(rec.get("id")) == str(approval_id):
            return rec
    return None


def set_status(approval_id: str, status: str) -> dict[str, Any] | None:
    """Mark an approval approved/rejected. Returns the updated record."""
    records = _read_all()
    # Collapse to latest-per-id while preserving order.
    seen: dict[str, dict[str, Any]] = {}
    for rec in records:
        seen[str(rec["id"])] = rec
    target = seen.get(str(approval_id))
    if target is None:
        return None
    target["status"] = status
    target["decided_at"] = _now()
    _write_all(list(seen.values()))
    return target


async def execute_approval(record: dict[str, Any], community_config: Any) -> dict[str, Any]:
    """Run an approved community action against the cloud.

    Returns a small result dict ``{"ok": bool, "detail": str}``. Raises nothing;
    failures are reported in the result so the caller can surface them.
    """
    from mira_engine.community.client import CommunityClient, CommunityError

    api_base = getattr(community_config, "api_base", "") or ""
    agent_token = getattr(community_config, "agent_token", "") or ""
    if not api_base or not agent_token:
        return {"ok": False, "detail": "not connected to the community (run login)"}

    action = record.get("action")
    payload = record.get("payload") or {}
    client = CommunityClient(api_base, agent_token)
    try:
        if action == "post_proposal":
            res = await client.create_proposal(payload.get("title", ""), payload.get("body", ""))
            return {"ok": True, "detail": f"proposal created ({res.get('id', '?')})"}
        if action == "comment":
            res = await client.post_comment(
                payload.get("thread_id", ""),
                payload.get("content", ""),
                payload.get("reply_to"),
            )
            return {"ok": True, "detail": f"comment posted ({res.get('comment_id', '?')})"}
        if action == "vote":
            res = await client.vote(
                payload.get("proposal_id", ""), int(payload.get("value", 1))
            )
            return {"ok": True, "detail": f"vote cast ({res.get('score', '?')})"}
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
