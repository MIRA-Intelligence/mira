"""Sync GitHub issue and pull request state changes back to CNB."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


CNB_API_URL = os.environ.get("CNB_API_URL", "https://api.cnb.cool").rstrip("/")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def event_payload() -> dict[str, Any]:
    path = env("GITHUB_EVENT_PATH")
    if not path:
        raise RuntimeError("GITHUB_EVENT_PATH is required")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub event payload must be a JSON object")
    return payload


def cnb_repo() -> str:
    explicit = env("CNB_TARGET_REPOSITORY") or env("CNB_REPOSITORY")
    if explicit:
        return explicit.strip("/")

    url = env("CNB_REPO_URL")
    if url:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if path:
            return path

    github_repo = env("GITHUB_REPOSITORY")
    if github_repo and "/" in github_repo:
        return f"mira-intelligence/{github_repo.split('/', 1)[1]}"

    raise RuntimeError("CNB_REPO_URL or CNB_TARGET_REPOSITORY is required")


def cnb_token() -> str:
    token = env("CNB_TOKEN") or env("CNB_GIT_TOKEN")
    if not token:
        raise RuntimeError("CNB_TOKEN or CNB_GIT_TOKEN is required")
    return token


def cnb_request(method: str, path: str, token: str, payload: dict[str, Any]) -> None:
    if env("CNB_SYNC_DRY_RUN"):
        print(f"DRY RUN {method} {path}: {json.dumps(payload, ensure_ascii=False)}")
        return

    request = urllib.request.Request(
        f"{CNB_API_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mira-github-cnb-metadata-sync",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CNB API {method} {path} failed: {exc.code} {details}") from exc


def marker_number(body: str, marker: str, fallback_path: str) -> str:
    patterns = [
        rf"{re.escape(marker)}:\s*([0-9]+)",
        rf"/-/{re.escape(fallback_path)}/([0-9]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            return match.group(1)
    return ""


def cnb_api_path(repo: str, kind: str, number: str) -> str:
    quoted_repo = urllib.parse.quote(repo.strip("/"), safe="/")
    return f"/{quoted_repo}/-/{kind}/{number}"


def sync_issue(payload: dict[str, Any]) -> int:
    issue = payload.get("issue") or {}
    if issue.get("pull_request"):
        print("Skipping issue event for pull request shadow issue")
        return 0

    number = marker_number(issue.get("body") or "", "CNB-Issue-IID", "issues")
    if not number:
        print(f"Skipping GitHub issue #{issue.get('number')}: no CNB issue marker found")
        return 0

    action = payload.get("action")
    if action not in {"closed", "reopened"}:
        print(f"Skipping unsupported issue action: {action or 'unknown'}")
        return 0

    if action == "closed":
        reason = issue.get("state_reason")
        if reason not in {"completed", "not_planned"}:
            reason = "completed"
        body = {"state": "closed", "state_reason": reason}
    else:
        body = {"state": "open", "state_reason": "reopened"}

    repo = cnb_repo()
    cnb_request("PATCH", cnb_api_path(repo, "issues", number), cnb_token(), body)
    print(f"Synced GitHub issue #{issue.get('number')} {action} to CNB issue #{number}")
    return 0


def sync_pull_request(payload: dict[str, Any]) -> int:
    pull = payload.get("pull_request") or {}
    number = marker_number(pull.get("body") or "", "CNB-PR-IID", "pulls")
    if not number:
        print(f"Skipping GitHub PR #{pull.get('number')}: no CNB PR marker found")
        return 0

    action = payload.get("action")
    if action not in {"closed", "reopened"}:
        print(f"Skipping unsupported pull request action: {action or 'unknown'}")
        return 0

    state = "closed" if action == "closed" else "open"
    repo = cnb_repo()
    cnb_request("PATCH", cnb_api_path(repo, "pulls", number), cnb_token(), {"state": state})
    print(f"Synced GitHub PR #{pull.get('number')} {action} to CNB PR #{number}")
    return 0


def main() -> int:
    payload = event_payload()
    event_name = env("GITHUB_EVENT_NAME")
    if event_name == "issues":
        return sync_issue(payload)
    if event_name == "pull_request":
        return sync_pull_request(payload)

    print(f"Skipping unsupported GitHub event: {event_name or 'unknown'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"GitHub to CNB metadata sync failed: {exc}", file=sys.stderr)
        raise
