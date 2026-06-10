"""Sync CNB issue events into the canonical GitHub issue tracker."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def github_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{GITHUB_API_URL}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mira-cnb-issue-sync",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {details}") from exc

    if not body:
        return {}
    return json.loads(body)


def target_repo() -> str:
    repo = env("GITHUB_TARGET_REPOSITORY") or env("GITHUB_REPOSITORY")
    return repo or "MIRA-Intelligence/mira"


def issue_marker() -> str:
    issue_id = env("CNB_ISSUE_ID")
    if not issue_id:
        raise RuntimeError("CNB_ISSUE_ID is required for issue synchronization")
    return f"CNB-Issue-ID: {issue_id}"


def issue_body() -> str:
    description = env("CNB_ISSUE_DESCRIPTION") or "_No CNB issue description provided._"
    source = env("CNB_EVENT_URL") or env("CNB_REPO_URL_HTTPS")
    visible_meta = [
        "",
        "---",
        "Synced from CNB.",
        f"- CNB issue: {source or 'unknown'}",
        f"- CNB author: {env('CNB_ISSUE_OWNER') or 'unknown'}",
        f"- CNB repo: {env('CNB_REPO_SLUG') or 'unknown'}",
    ]
    hidden_meta = [
        "<!-- cnb-sync",
        issue_marker(),
        f"CNB-Issue-IID: {env('CNB_ISSUE_IID')}",
        f"CNB-Repo: {env('CNB_REPO_SLUG')}",
        "-->",
    ]
    return "\n".join([description, *visible_meta, "", *hidden_meta])


def issue_title() -> str:
    title = env("CNB_ISSUE_TITLE") or f"CNB issue {env('CNB_ISSUE_IID') or env('CNB_ISSUE_ID')}"
    iid = env("CNB_ISSUE_IID")
    if iid and not title.startswith(f"[CNB #{iid}]"):
        return f"[CNB #{iid}] {title}"
    return title


def desired_state() -> str:
    event = env("CNB_EVENT")
    state = env("CNB_ISSUE_STATE").lower()
    if event == "issue.close" or state == "closed":
        return "closed"
    if event == "issue.reopen" or state == "open":
        return "open"
    return "open"


def find_github_issue(repo: str, token: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode(
        {"q": f'repo:{repo} is:issue "{issue_marker()}" in:body'}
    )
    result = github_request("GET", f"/search/issues?{query}", token)
    assert isinstance(result, dict)
    items = result.get("items") or []
    return items[0] if items else None


def sync_issue(repo: str, token: str) -> dict[str, Any]:
    existing = find_github_issue(repo, token)
    payload = {
        "title": issue_title(),
        "body": issue_body(),
        "state": desired_state(),
    }

    if existing:
        number = existing["number"]
        updated = github_request("PATCH", f"/repos/{repo}/issues/{number}", token, payload)
        assert isinstance(updated, dict)
        print(f"Updated GitHub issue #{number}")
        return updated

    created = github_request(
        "POST",
        f"/repos/{repo}/issues",
        token,
        {"title": payload["title"], "body": payload["body"]},
    )
    assert isinstance(created, dict)
    number = created["number"]
    print(f"Created GitHub issue #{number}")

    if payload["state"] == "closed":
        updated = github_request(
            "PATCH",
            f"/repos/{repo}/issues/{number}",
            token,
            {"state": "closed"},
        )
        assert isinstance(updated, dict)
        return updated
    return created


def comment_exists(repo: str, token: str, issue_number: int, marker: str) -> bool:
    path = f"/repos/{repo}/issues/{issue_number}/comments?per_page=100"
    comments = github_request("GET", path, token)
    assert isinstance(comments, list)
    return any(marker in (comment.get("body") or "") for comment in comments)


def sync_comment(repo: str, token: str, issue: dict[str, Any]) -> None:
    comment_id = env("CNB_COMMENT_ID")
    comment_body = env("CNB_COMMENT_BODY")
    if not comment_id or not comment_body:
        print("No CNB comment payload found; skipping comment sync")
        return

    marker = f"CNB-Comment-ID: {comment_id}"
    issue_number = int(issue["number"])
    if comment_exists(repo, token, issue_number, marker):
        print(f"GitHub issue #{issue_number} already has CNB comment {comment_id}")
        return

    body = "\n".join(
        [
            f"CNB comment from {env('CNB_BUILD_USER') or env('CNB_ISSUE_OWNER') or 'unknown'}:",
            "",
            comment_body,
            "",
            "---",
            "<!-- cnb-sync",
            marker,
            f"CNB-Issue-ID: {env('CNB_ISSUE_ID')}",
            "-->",
        ]
    )
    github_request(
        "POST",
        f"/repos/{repo}/issues/{issue_number}/comments",
        token,
        {"body": body},
    )
    print(f"Created comment on GitHub issue #{issue_number}")


def main() -> int:
    token = env("GITHUB_SYNC_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_SYNC_TOKEN is required")

    event = env("CNB_EVENT")
    if not event.startswith("issue."):
        print(f"Skipping non-issue event: {event or 'unknown'}")
        return 0

    repo = target_repo()
    issue = sync_issue(repo, token)
    if event == "issue.comment":
        sync_comment(repo, token, issue)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"CNB issue sync failed: {exc}", file=sys.stderr)
        raise
