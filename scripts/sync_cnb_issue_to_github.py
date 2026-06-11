"""Sync CNB issue and pull request events into the canonical GitHub tracker."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


class GitHubApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, details: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.details = details
        super().__init__(f"GitHub API {method} {path} failed: {status} {details}")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def first_env(names: list[str], default: str = "") -> str:
    for name in names:
        value = env(name)
        if value:
            return value
    return default


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
            "User-Agent": "mira-cnb-metadata-sync",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(method, path, exc.code, details) from exc

    if not body:
        return {}
    return json.loads(body)


def target_repo() -> str:
    repo = env("GITHUB_TARGET_REPOSITORY") or env("GITHUB_REPOSITORY")
    return repo or "MIRA-Intelligence/mira"


def repo_owner(repo: str) -> str:
    return repo.split("/", 1)[0]


def event_name() -> str:
    event = first_env(["CNB_EVENT", "CNB_BUILD_EVENT", "CNB_EVENT_NAME", "CNB_TRIGGER_EVENT"])
    if event:
        return event
    if env("CNB_PULL_REQUEST_ID") or env("CNB_PULL_REQUEST_IID"):
        return "pull_request.comment" if env("CNB_COMMENT_ID") else "pull_request"
    if env("CNB_ISSUE_ID") or env("CNB_ISSUE_IID"):
        return "issue.comment" if env("CNB_COMMENT_ID") else "issue.open"
    return ""


def strip_ref(branch: str) -> str:
    return branch.removeprefix("refs/heads/")


def issue_id() -> str:
    value = env("CNB_ISSUE_ID") or env("CNB_ISSUE_IID")
    if not value:
        raise RuntimeError("CNB_ISSUE_ID is required for issue synchronization")
    return value


def issue_marker() -> str:
    return f"CNB-Issue-ID: {issue_id()}"


def issue_body() -> str:
    description = env("CNB_ISSUE_DESCRIPTION") or "_No CNB issue description provided._"
    source = first_env(["CNB_EVENT_URL", "CNB_ISSUE_URL", "CNB_REPO_URL_HTTPS"])
    visible_meta = [
        "",
        "---",
        "Synced from CNB.",
        f"- CNB issue: {source or 'unknown'}",
        f"- CNB author: {env('CNB_ISSUE_OWNER') or env('CNB_BUILD_USER') or 'unknown'}",
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
    title = env("CNB_ISSUE_TITLE") or f"CNB issue {env('CNB_ISSUE_IID') or issue_id()}"
    iid = env("CNB_ISSUE_IID")
    if iid and not title.startswith(f"[CNB #{iid}]"):
        return f"[CNB #{iid}] {title}"
    return title


def desired_issue_state(event: str) -> str:
    state = env("CNB_ISSUE_STATE").lower()
    if event == "issue.close" or state == "closed":
        return "closed"
    if event == "issue.reopen" or state == "open":
        return "open"
    return "open"


def search_issue_or_pr(repo: str, token: str, kind: str, marker: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"q": f'repo:{repo} is:{kind} "{marker}" in:body'})
    result = github_request("GET", f"/search/issues?{query}", token)
    assert isinstance(result, dict)
    items = result.get("items") or []
    return items[0] if items else None


def find_github_issue(repo: str, token: str) -> dict[str, Any] | None:
    return search_issue_or_pr(repo, token, "issue", issue_marker())


def sync_issue(repo: str, token: str, event: str) -> dict[str, Any]:
    existing = find_github_issue(repo, token)
    payload = {
        "title": issue_title(),
        "body": issue_body(),
        "state": desired_issue_state(event),
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


def comment_id() -> str:
    return first_env(["CNB_COMMENT_ID", "CNB_COMMENT_IID"])


def comment_body() -> str:
    return first_env(
        [
            "CNB_COMMENT_BODY",
            "CNB_COMMENT_CONTENT",
            "CNB_COMMENT_DESCRIPTION",
            "CNB_COMMENT_TEXT",
        ]
    )


def comment_exists(repo: str, token: str, issue_number: int, marker: str) -> bool:
    path = f"/repos/{repo}/issues/{issue_number}/comments?per_page=100"
    comments = github_request("GET", path, token)
    assert isinstance(comments, list)
    return any(marker in (comment.get("body") or "") for comment in comments)


def post_issue_comment(
    repo: str,
    token: str,
    issue_number: int,
    marker: str,
    body_lines: list[str],
) -> None:
    if comment_exists(repo, token, issue_number, marker):
        print(f"GitHub thread #{issue_number} already has marker {marker}")
        return

    body = "\n".join(
        [
            *body_lines,
            "",
            "---",
            "<!-- cnb-sync",
            marker,
            f"CNB-Repo: {env('CNB_REPO_SLUG')}",
            "-->",
        ]
    )
    github_request(
        "POST",
        f"/repos/{repo}/issues/{issue_number}/comments",
        token,
        {"body": body},
    )
    print(f"Created comment on GitHub thread #{issue_number}")


def sync_issue_comment(repo: str, token: str, issue: dict[str, Any]) -> None:
    cnb_comment_id = comment_id()
    cnb_comment_body = comment_body()
    if not cnb_comment_id or not cnb_comment_body:
        print("No CNB issue comment payload found; skipping comment sync")
        return

    marker = f"CNB-Comment-ID: {cnb_comment_id}"
    issue_number = int(issue["number"])
    post_issue_comment(
        repo,
        token,
        issue_number,
        marker,
        [
            f"CNB issue comment from {env('CNB_BUILD_USER') or env('CNB_ISSUE_OWNER') or 'unknown'}:",
            "",
            cnb_comment_body,
        ],
    )


def pr_id() -> str:
    value = env("CNB_PULL_REQUEST_ID") or env("CNB_PULL_REQUEST_IID")
    if not value:
        raise RuntimeError("CNB_PULL_REQUEST_ID is required for pull request synchronization")
    return value


def pr_marker() -> str:
    return f"CNB-PR-ID: {pr_id()}"


def pr_head_branch() -> str:
    return strip_ref(
        first_env(
            [
                "CNB_PULL_REQUEST_BRANCH",
                "CNB_PULL_REQUEST_SOURCE_BRANCH",
                "CNB_SOURCE_BRANCH",
                "CNB_HEAD_BRANCH",
            ]
        )
    )


def pr_base_branch() -> str:
    branch = strip_ref(
        first_env(
            [
                "CNB_PULL_REQUEST_TARGET_BRANCH",
                "CNB_PULL_REQUEST_BASE_BRANCH",
                "CNB_TARGET_BRANCH",
                "CNB_BASE_BRANCH",
                "CNB_BRANCH",
            ],
            "dev",
        )
    )
    if branch == pr_head_branch() or branch.startswith("cnb/"):
        return "dev"
    return branch


def pr_title() -> str:
    title = env("CNB_PULL_REQUEST_TITLE") or f"CNB PR {env('CNB_PULL_REQUEST_IID') or pr_id()}"
    iid = env("CNB_PULL_REQUEST_IID")
    if iid and not title.startswith(f"[CNB PR #{iid}]"):
        return f"[CNB PR #{iid}] {title}"
    return title


def pr_body() -> str:
    description = env("CNB_PULL_REQUEST_DESCRIPTION") or "_No CNB pull request description provided._"
    source = first_env(["CNB_PULL_REQUEST_URL", "CNB_EVENT_URL", "CNB_REPO_URL_HTTPS"])
    visible_meta = [
        "",
        "---",
        "Synced from CNB.",
        f"- CNB PR: {source or 'unknown'}",
        f"- CNB author: {env('CNB_PULL_REQUEST_AUTHOR') or env('CNB_BUILD_USER') or 'unknown'}",
        f"- CNB source: {env('CNB_PULL_REQUEST_SOURCE_SLUG') or env('CNB_REPO_SLUG') or 'unknown'}",
        f"- CNB target: {env('CNB_PULL_REQUEST_TARGET_SLUG') or env('CNB_REPO_SLUG') or 'unknown'}",
        f"- GitHub head branch: `{pr_head_branch() or 'unknown'}`",
        f"- GitHub base branch: `{pr_base_branch()}`",
    ]
    hidden_meta = [
        "<!-- cnb-sync",
        pr_marker(),
        f"CNB-PR-IID: {env('CNB_PULL_REQUEST_IID')}",
        f"CNB-Repo: {env('CNB_REPO_SLUG')}",
        "-->",
    ]
    return "\n".join([description, *visible_meta, "", *hidden_meta])


def find_github_pr_by_marker(repo: str, token: str) -> dict[str, Any] | None:
    return search_issue_or_pr(repo, token, "pr", pr_marker())


def find_github_pr_by_branch(repo: str, token: str) -> dict[str, Any] | None:
    head_branch = pr_head_branch()
    if not head_branch:
        return None

    params = urllib.parse.urlencode(
        {
            "state": "open",
            "head": f"{repo_owner(repo)}:{head_branch}",
            "base": pr_base_branch(),
            "per_page": "10",
        }
    )
    pulls = github_request("GET", f"/repos/{repo}/pulls?{params}", token)
    assert isinstance(pulls, list)
    return pulls[0] if pulls else None


def find_github_pr(repo: str, token: str) -> dict[str, Any] | None:
    return find_github_pr_by_marker(repo, token) or find_github_pr_by_branch(repo, token)


def create_github_pr(repo: str, token: str) -> dict[str, Any] | None:
    head_branch = pr_head_branch()
    if not head_branch:
        print("No CNB pull request source branch found; skipping GitHub PR creation")
        return None

    try:
        created = github_request(
            "POST",
            f"/repos/{repo}/pulls",
            token,
            {
                "title": pr_title(),
                "head": head_branch,
                "base": pr_base_branch(),
                "body": pr_body(),
                "maintainer_can_modify": True,
            },
        )
    except GitHubApiError as exc:
        if exc.status == 422:
            print(
                "Could not create GitHub PR, likely because the mirrored CNB branch "
                f"`{head_branch}` is not on GitHub yet: {exc.details}"
            )
            return None
        raise

    assert isinstance(created, dict)
    print(f"Created GitHub PR #{created['number']}")
    return created


def sync_pull_request(repo: str, token: str) -> dict[str, Any] | None:
    existing = find_github_pr(repo, token)
    payload = {
        "title": pr_title(),
        "body": pr_body(),
        "base": pr_base_branch(),
    }

    if existing:
        number = existing["number"]
        updated = github_request("PATCH", f"/repos/{repo}/pulls/{number}", token, payload)
        assert isinstance(updated, dict)
        print(f"Updated GitHub PR #{number}")
        return updated

    return create_github_pr(repo, token)


def sync_pr_opening_comment(repo: str, token: str, pull: dict[str, Any]) -> None:
    description = env("CNB_PULL_REQUEST_DESCRIPTION")
    if not description:
        print("No CNB PR description found; skipping opening comment sync")
        return

    marker = f"CNB-PR-Opening-Comment-ID: {pr_id()}"
    issue_number = int(pull["number"])
    source = first_env(["CNB_PULL_REQUEST_URL", "CNB_EVENT_URL"])
    lines = [
        f"CNB PR opening comment from {env('CNB_PULL_REQUEST_AUTHOR') or env('CNB_BUILD_USER') or 'unknown'}:",
        "",
        description,
    ]
    if source:
        lines.extend(["", f"CNB source: {source}"])
    post_issue_comment(repo, token, issue_number, marker, lines)


def sync_pr_comment(repo: str, token: str, pull: dict[str, Any]) -> None:
    cnb_comment_id = comment_id()
    cnb_comment_body = comment_body()
    if not cnb_comment_id or not cnb_comment_body:
        print("No CNB PR comment payload found; skipping comment sync")
        return

    marker = f"CNB-PR-Comment-ID: {cnb_comment_id}"
    issue_number = int(pull["number"])
    lines = [
        f"CNB PR comment from {env('CNB_BUILD_USER') or env('CNB_PULL_REQUEST_AUTHOR') or 'unknown'}:",
        "",
        cnb_comment_body,
    ]
    source = env("CNB_EVENT_URL")
    if source:
        lines.extend(["", f"CNB source: {source}"])
    post_issue_comment(repo, token, issue_number, marker, lines)


def main() -> int:
    token = env("GH_SYNC_TOKEN") or env("GITHUB_SYNC_TOKEN")
    if not token:
        raise RuntimeError("GH_SYNC_TOKEN is required")

    event = event_name()
    repo = target_repo()
    print(f"Handling CNB event `{event or 'unknown'}` for GitHub repo `{repo}`")

    if event.startswith("issue."):
        issue = sync_issue(repo, token, event)
        if event == "issue.comment":
            sync_issue_comment(repo, token, issue)
        return 0

    if event.startswith("pull_request"):
        pull = sync_pull_request(repo, token)
        if pull is None:
            return 0
        if event in {"pull_request", "pull_request.update"}:
            sync_pr_opening_comment(repo, token, pull)
        if event == "pull_request.comment":
            sync_pr_comment(repo, token, pull)
        return 0

    print(f"Skipping unsupported CNB event: {event or 'unknown'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"CNB metadata sync failed: {exc}", file=sys.stderr)
        raise
