#!/usr/bin/env python3
"""Smoke checks for release-train workflow combinations."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request


def _fetch_json(url: str, timeout: float = 5.0, *, token: str | None = None) -> tuple[int, dict]:
    # The gateway requires its token on /api/* (health and version are exempt).
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            return resp.status, data if isinstance(data, dict) else {}
    except urllib.error.HTTPError as exc:
        # Keep the status: a 401 should read as an auth failure, not "unreachable".
        return exc.code, {}
    except (urllib.error.URLError, json.JSONDecodeError):
        return 0, {}


def run(
    base_url: str,
    *,
    expected_agent: str | None = None,
    expected_contract: str | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    checks = {}
    http: dict[str, int] = {}

    def fetch(path: str) -> dict:
        status, data = _fetch_json(f"{base_url}{path}", token=token)
        http[path] = status
        return data

    health = fetch("/health")
    checks["desktop_local_health"] = http["/health"] == 200 and health.get("status") == "ok"

    version = fetch("/version")
    checks["desktop_local_version"] = (
        http["/version"] == 200
        and isinstance(version.get("agent_version"), str)
        and isinstance(version.get("api_contract"), str)
    )

    status = fetch("/api/status")
    checks["desktop_cloud_api_status"] = http["/api/status"] == 200 and "connected_clients" in status

    # For now, web-cloud smoke uses the same gateway REST contract probe.
    checks["web_cloud_api_status"] = http["/api/status"] == 200 and "channel" in status

    if expected_agent is not None:
        checks["engine_release_version"] = version.get("agent_version") == expected_agent
        contract = fetch("/api/workflow/v1/contract")
        checks["modular_workflow_contract"] = http["/api/workflow/v1/contract"] == 200 and set(
            contract.get("module_types", [])
        ) == {"initiation", "planning", "experiment", "delivery"}
    if expected_contract is not None:
        checks["pinned_api_contract"] = version.get("api_contract") == expected_contract

    ok = all(checks.values())
    report = {
        "ok": ok,
        "base_url": base_url,
        "checks": checks,
        "http": http,
        "health": health,
        "version": version,
        "status": status,
    }
    return (0 if ok else 1), report


def main() -> int:
    parser = argparse.ArgumentParser(description="Release train smoke checks")
    parser.add_argument("--base-url", default="http://127.0.0.1:18790")
    parser.add_argument("--expected-agent")
    parser.add_argument("--expected-contract")
    parser.add_argument(
        "--token",
        default=os.environ.get("MIRA_GATEWAY_TOKEN"),
        help="gateway token for /api/* (default: $MIRA_GATEWAY_TOKEN)",
    )
    args = parser.parse_args()

    code, report = run(
        args.base_url.rstrip("/"),
        expected_agent=args.expected_agent,
        expected_contract=args.expected_contract,
        token=args.token,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
