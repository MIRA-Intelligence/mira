#!/usr/bin/env python3
"""Validate compatibility.json contract used by release train workflows."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RELEASE_TRAIN_RE = re.compile(r"^\d{4}\.(0[1-9]|1[0-2])(?:rc\d+)?$")
VERSION_SPEC_RE = re.compile(r"^(?:\d+\.\d+\.x|\d+\.\d+\.\d+(?:rc\d+)?)$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:rc\d+)?$")
API_CONTRACT_RE = re.compile(r"^v\d+$")

REQUIRED_KEYS = {
    "release_train",
    "ui",
    "agent",
    "api_contract",
    "min_agent_for_ui",
}


def _validate_data(payload: dict[str, object]) -> list[str]:
    errors: list[str] = []

    missing = sorted(REQUIRED_KEYS - set(payload))
    if missing:
        errors.append(f"Missing required key(s): {', '.join(missing)}")

    unexpected = sorted(set(payload) - REQUIRED_KEYS)
    if unexpected:
        errors.append(f"Unknown key(s): {', '.join(unexpected)}")

    release_train = payload.get("release_train")
    if not isinstance(release_train, str) or not RELEASE_TRAIN_RE.fullmatch(release_train):
        errors.append("release_train must match YYYY.MM or YYYY.MMrcN (e.g. 2026.04, 2026.04rc1).")

    ui = payload.get("ui")
    if not isinstance(ui, str) or not VERSION_SPEC_RE.fullmatch(ui):
        errors.append("ui must match major.minor.x or major.minor.patchrcN (e.g. 2.3.x, 2.3.0rc1).")

    agent = payload.get("agent")
    if not isinstance(agent, str) or not VERSION_SPEC_RE.fullmatch(agent):
        errors.append("agent must match major.minor.x or major.minor.patchrcN (e.g. 1.6.x, 1.6.0rc1).")

    api_contract = payload.get("api_contract")
    if not isinstance(api_contract, str) or not API_CONTRACT_RE.fullmatch(api_contract):
        errors.append("api_contract must match v<number> (e.g. v1).")

    min_agent = payload.get("min_agent_for_ui")
    if not isinstance(min_agent, str) or not SEMVER_RE.fullmatch(min_agent):
        errors.append("min_agent_for_ui must match semantic version major.minor.patch or major.minor.patchrcN.")

    return errors


def _check_agent_matches_tag(payload: dict[str, object], required: str) -> list[str]:
    """Return errors when ``compatibility.json#agent`` does not satisfy ``required``.

    ``required`` is the version string parsed from the release tag (no leading
    ``v``). Two pinning styles are accepted on the file side:

    * Exact version (e.g. ``0.2.0rc4``) — must be string-equal to ``required``.
    * Minor range (e.g. ``0.2.x``) — ``required`` is accepted iff its
      ``major.minor`` matches and the patch suffix exists. This lets the same
      guard work both pre-GA (when each rc pins a specific build) and post-GA
      (when the file moves to ``0.2.x`` style after the user's planned
      schema migration).
    """
    pinned = payload.get("agent")
    if not isinstance(pinned, str):
        return ["compatibility.json#agent is missing or not a string."]

    if not SEMVER_RE.fullmatch(required):
        return [
            f"--require-agent expected a semantic version like 0.2.0 or 0.2.0rc1, got {required!r}."
        ]

    if pinned.endswith(".x"):
        # Range form: "0.2.x" matches any 0.2.* (including rc suffixes).
        prefix = pinned[:-1]  # "0.2."
        if not required.startswith(prefix):
            return [
                f"tag v{required} does not fall in compatibility.json#agent range "
                f"{pinned!r}. Bump the agent range or fix the tag before releasing."
            ]
        return []

    # Exact form: must be byte-for-byte equal.
    if pinned != required:
        return [
            f"tag v{required} does not match compatibility.json#agent={pinned!r}. "
            "Update compatibility.json (and bump release_train if needed) before tagging."
        ]
    return []


def validate_file(path: Path, *, require_agent: str | None = None) -> int:
    if not path.exists():
        print(f"compatibility file not found: {path}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid JSON in {path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print(f"expected JSON object in {path}", file=sys.stderr)
        return 1

    errors = _validate_data(payload)
    if not errors and require_agent is not None:
        errors.extend(_check_agent_matches_tag(payload, require_agent))

    if errors:
        print("compatibility validation failed:", file=sys.stderr)
        for msg in errors:
            print(f"- {msg}", file=sys.stderr)
        return 1

    if require_agent is not None:
        print(
            f"compatibility validation passed: {path} "
            f"(agent matches tag v{require_agent})"
        )
    else:
        print(f"compatibility validation passed: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Mira compatibility.json")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path("compatibility.json"),
        help="Path to compatibility mapping file.",
    )
    parser.add_argument(
        "--require-agent",
        default=None,
        help=(
            "Optional. Assert that compatibility.json#agent satisfies this version "
            "(e.g. 0.2.0rc4, parsed from a release tag without the leading 'v'). "
            "Accepts both exact pins and minor-range entries (0.2.x). Used by the "
            "Agent Release workflow to fail fast when the file is stale."
        ),
    )
    args = parser.parse_args()
    return validate_file(args.file, require_agent=args.require_agent)


if __name__ == "__main__":
    raise SystemExit(main())
