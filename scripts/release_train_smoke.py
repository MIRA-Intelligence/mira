#!/usr/bin/env python3
"""Smoke checks for release-train workflow combinations."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def _fetch_json(url: str, timeout: float = 5.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            return resp.status, data if isinstance(data, dict) else {}
    except (urllib.error.URLError, json.JSONDecodeError):
        return 0, {}


def run(base_url: str, *, expected_agent: str | None = None, expected_contract: str | None = None) -> tuple[int, dict]:
    checks = {}

    health_status, health = _fetch_json(f"{base_url}/health")
    checks["desktop_local_health"] = health_status == 200 and health.get("status") == "ok"

    version_status, version = _fetch_json(f"{base_url}/version")
    checks["desktop_local_version"] = (
        version_status == 200
        and isinstance(version.get("agent_version"), str)
        and isinstance(version.get("api_contract"), str)
    )

    status_status, status = _fetch_json(f"{base_url}/api/status")
    checks["desktop_cloud_api_status"] = status_status == 200 and "connected_clients" in status

    # For now, web-cloud smoke uses the same gateway REST contract probe.
    checks["web_cloud_api_status"] = status_status == 200 and "channel" in status

    if expected_agent is not None:
        checks["engine_release_version"] = version.get("agent_version") == expected_agent
        contract_status, contract = _fetch_json(f"{base_url}/api/workflow/v1/contract")
        checks["modular_workflow_contract"] = contract_status == 200 and set(contract.get("module_types", [])) == {
            "initiation", "planning", "experiment", "delivery",
        }
    if expected_contract is not None:
        checks["pinned_api_contract"] = version.get("api_contract") == expected_contract

    ok = all(checks.values())
    report = {
        "ok": ok,
        "base_url": base_url,
        "checks": checks,
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
    args = parser.parse_args()

    code, report = run(args.base_url.rstrip("/"), expected_agent=args.expected_agent, expected_contract=args.expected_contract)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
