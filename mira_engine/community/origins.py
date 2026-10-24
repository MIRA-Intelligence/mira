"""Where a community action was asked for, so its outcome is reported there (#72).

A proposal the agent posts and a PR it opens come from some conversation — a
UI chat, Telegram, the CLI. The platform's later news about them (a claim, a
review, a merge, a revert, a rejection) arrives on the community channel,
which has no user to tell. This file maps proposal and PR ids to the
conversation they came from, so the channel can hand the news back to the chat
it started in instead of leaving it in the UI-only inbox.

Stored as JSON under the instance data dir (``community_origins.json``);
bounded, never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.config.paths import get_data_dir
from mira_engine.utils.locks import locked_update_text

ORIGINS_FILENAME = "community_origins.json"
MAX_PER_KIND = 300
KINDS = ("proposal", "pr")
# Follow-up pushes to one PR (a CI fix, a rebase, a review's changes) run
# without a fresh approval in hybrid mode — up to this many; the next one is
# held so a human looks at a PR that keeps missing (mira-community#72).
MAX_AUTO_FIX_ROUNDS = 3

# Platform events about a PR the agent opened / a proposal it posted.
_PR_EVENTS = {
    "pr_registered": "was registered with the community",
    "pr_policy": "breaks a contribution boundary (see the details)",
    "pr_reviewed": "was reviewed",
    "pr_rejected": "was rejected",
    "pr_merged": "was merged into the landing branch",
    "pr_landed": "landed: the landing branch is green with it",
    "pr_reverted": "was reverted after landing (the landing branch went red)",
    "pr_ci_failed": "failed CI",
    "pr_ci_passed": "passed CI again after your fix",
    "pr_conflict": "no longer merges cleanly into the landing branch",
    "pr_closed": "was closed on the code host without merging",
}
_PROPOSAL_EVENTS = {
    "proposal_claimed": "is now being built by {claimant}",
    "proposal_claim_released": "is open again: its builder released or lost the claim",
    "proposal_pr_registered": "has a pull request: {url}",
    "proposal_pr_merged": "was built and merged into the landing branch ({url})",
    "proposal_pr_landed": "has landed: the landing branch is green with it",
    "proposal_pr_reverted": "was reverted after landing; it is open to be rebuilt",
    "proposal_rejected": "was voted down by the community (score {score}) and is rejected",
}


def origins_path() -> Path:
    return get_data_dir() / ORIGINS_FILENAME


def _load(text: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: (v if isinstance(v, dict) else {}) for k, v in ((kind, data.get(kind)) for kind in KINDS)}


def remember(kind: str, item_id: str, origin: dict[str, str] | None, **extra: Any) -> None:
    """Record that ``item_id`` (a proposal or PR id) came from ``origin``
    (``channel``/``chat_id``[/``project_id``]). No origin → nothing to remember."""
    if kind not in KINDS or not item_id or not origin or not origin.get("channel") or not origin.get("chat_id"):
        return
    entry = {"origin": dict(origin), "at": datetime.now(timezone.utc).isoformat(), **extra}

    def update(text: str) -> str:
        data = _load(text)
        bucket = data[kind]
        bucket[str(item_id)] = entry
        for stale in list(bucket)[: max(0, len(bucket) - MAX_PER_KIND)]:
            del bucket[stale]
        return json.dumps(data, ensure_ascii=False, indent=2)

    try:
        path = origins_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_update_text(path, update)
    except Exception as e:  # noqa: BLE001 - a lost origin only costs a follow-up
        logger.debug("Could not record the community origin of {} {}: {}", kind, item_id, e)


def lookup(kind: str, item_id: str) -> dict[str, Any] | None:
    if kind not in KINDS or not item_id:
        return None
    try:
        data = _load(origins_path().read_text(encoding="utf-8"))
    except OSError:
        return None
    entry = data[kind].get(str(item_id))
    return entry if isinstance(entry, dict) and isinstance(entry.get("origin"), dict) else None


def pr_for_working_dir(working_dir: str) -> tuple[str, dict[str, Any]] | None:
    """The PR the agent opened from a contribution workspace, as (pr_id, entry);
    the most recent one when the workspace produced several."""
    if not working_dir:
        return None
    try:
        data = _load(origins_path().read_text(encoding="utf-8"))
    except OSError:
        return None
    want = str(Path(working_dir).expanduser())
    found = [
        (pr_id, entry)
        for pr_id, entry in data["pr"].items()
        if isinstance(entry, dict) and str(Path(str(entry.get("working_dir") or "")).expanduser()) == want
    ]
    if not found:
        return None
    return max(found, key=lambda item: str(item[1].get("at") or ""))


def update(kind: str, item_id: str, **fields: Any) -> None:
    """Merge ``fields`` into a remembered entry (e.g. the fix-round counter); a
    missing entry is left alone."""
    if kind not in KINDS or not item_id or not fields:
        return

    def apply(text: str) -> str:
        data = _load(text)
        entry = data[kind].get(str(item_id))
        if isinstance(entry, dict):
            entry.update(fields)
        return json.dumps(data, ensure_ascii=False, indent=2)

    try:
        locked_update_text(origins_path(), apply)
    except Exception as e:  # noqa: BLE001 - a lost counter only costs an extra approval
        logger.debug("Could not update the community origin of {} {}: {}", kind, item_id, e)


def _check_names(event: dict[str, Any]) -> list[str]:
    """The failing checks an event names, each with its page when given."""
    names: list[str] = []
    for check in event.get("checks") or []:
        if isinstance(check, dict) and check.get("name"):
            names.append(f"{check['name']} ({check['url']})" if check.get("url") else str(check["name"]))
        elif isinstance(check, str) and check:
            names.append(check)
    return names


def _plugin_install_hint(entry: dict[str, Any]) -> str:
    ids = [str(p) for p in (entry.get("plugin_ids") or []) if p]
    if not ids:
        return ""
    where = str(entry.get("working_dir") or "the contribution workspace")
    return (
        f" It added the plugin(s) {', '.join(ids)}: offer to install them locally — copy "
        f"`{where}/plugins/<id>` to `~/.mira/plugins/<id>`, then your user runs `mira plugin enable <id>` "
        "and restarts the gateway (an unrated plugin runs out of process, sandboxed, and may only provide tools)."
    )


def origin_follow_up(event: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    """The prompt to hand back to the conversation an event's PR or proposal
    came from, and that conversation; None when the event is about something
    the agent did not start from a chat."""
    etype = str(event.get("type") or "")
    if etype in _PR_EVENTS:
        pr_id = str(event.get("pr_id") or "")
        entry = lookup("pr", pr_id)
        if not entry:
            return None
        what = f"the pull request {entry.get('url') or pr_id}"
        news = _PR_EVENTS[etype]
        detail = ""
        if etype == "pr_reviewed":
            detail = f" ({event.get('verdict') or 'see the review'})"
        elif etype == "pr_policy":
            problems = event.get("problems") or []
            detail = ": " + "; ".join(str(p) for p in problems) if problems else ""
        elif etype == "pr_rejected" and event.get("reason"):
            detail = f": {event.get('reason')}"
        elif etype == "pr_ci_failed":
            names = _check_names(event)
            detail = f": {', '.join(names)}" if names else ""
        tail = _plugin_install_hint(entry) if etype in ("pr_merged", "pr_landed") else ""
        workspace = str(entry.get("working_dir") or "the contribution workspace")
        if etype == "pr_reviewed" and str(event.get("verdict") or "") == "request_changes":
            tail = (
                f" Read the review (community_review_pr with pr_id {pr_id}), address it in the contribution "
                f"workspace `{workspace}` and call community_open_pr again with the same working_dir: it pushes "
                "the follow-up commits to the same PR."
            )
        elif etype == "pr_ci_failed":
            tail = (
                f" Call community_pr_status with pr_id {pr_id} for each failing check's log excerpt and the "
                f"next step. Fix the cause in the contribution workspace `{workspace}`, run the project's tests "
                "there, then call community_open_pr again with the same working_dir and title: it pushes the "
                "fix to this PR. If the failure is unrelated to this change (flaky, or already failing on the "
                "landing branch), say so to your user instead of pushing."
            )
        elif etype == "pr_conflict":
            landing = str(event.get("landing_branch") or "the landing branch")
            tail = (
                f" Update the workspace branch from the landing branch (`git fetch origin && git merge "
                f"origin/{landing}` in `{workspace}`), resolve the conflicts, run the tests, then call "
                "community_open_pr again with the same working_dir."
            )
        elif etype == "pr_ci_passed":
            tail = " Nothing to do now: it waits for reviews or a maintainer."
        elif etype == "pr_closed":
            tail = (
                " Ask your user whether to drop the change or reopen it (community_open_pr from the same "
                "workspace opens a new PR)."
            )
    elif etype in _PROPOSAL_EVENTS:
        proposal_id = str(event.get("proposal_id") or "")
        entry = lookup("proposal", proposal_id)
        if not entry:
            return None
        title = str(event.get("title") or entry.get("title") or proposal_id)
        what = f'the proposal "{title}"'
        news = _PROPOSAL_EVENTS[etype].format(
            claimant=event.get("claimant") or event.get("actor") or "another member",
            url=event.get("url") or event.get("pr_url") or "a registered PR",
            score=event.get("score", "?"),
        )
        detail, tail = "", ""
    else:
        return None
    text = (
        f"[Community] News about {what} you started from this conversation: it {news}{detail}. "
        f"Tell your user briefly, in their language.{tail} Your final message here is not posted to the community."
    )
    return text, dict(entry["origin"])
