#!/usr/bin/env python3
"""Local, non-mutating preflight checks for a paired Mira release."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:rc\d+)?$")


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{' '.join(args)} failed in {cwd}: {detail}")
    return result.stdout.strip()


def require_version(value: str, label: str) -> str:
    normalized = value.removeprefix("v")
    if not VERSION_RE.fullmatch(normalized):
        raise ValueError(f"{label} must match X.Y.Z or X.Y.ZrcN: {value!r}")
    return normalized


def require_clean(repo: Path) -> None:
    status = run("git", "status", "--porcelain", cwd=repo)
    if status:
        raise RuntimeError(f"worktree is not clean: {repo}\n{status}")


def require_ref(repo: Path, ref: str) -> None:
    run("git", "rev-parse", "--verify", ref, cwd=repo)


def require_absent_tag(repo: Path, tag: str) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
        cwd=repo,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        raise RuntimeError(f"tag already exists in {repo}: {tag}")


def engine_contract(mira_repo: Path) -> str:
    source = (mira_repo / "mira_engine/channels/ui.py").read_text(encoding="utf-8")
    match = re.search(r'^_API_CONTRACT_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if not match:
        raise RuntimeError("could not resolve _API_CONTRACT_VERSION")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-version", required=True)
    parser.add_argument("--ui-version", required=True)
    parser.add_argument("--mira-repo", type=Path, default=Path("/Users/cwang/Code/mira"))
    parser.add_argument("--ui-repo", type=Path, default=Path("/Users/cwang/Code/mira-ui"))
    args = parser.parse_args()

    agent = require_version(args.agent_version, "agent version")
    ui = require_version(args.ui_version, "UI version")
    mira_repo = args.mira_repo.expanduser().resolve()
    ui_repo = args.ui_repo.expanduser().resolve()

    for repo in (mira_repo, ui_repo):
        require_clean(repo)
        require_ref(repo, "origin/dev")
        require_ref(repo, "origin/release")

    require_absent_tag(mira_repo, f"v{agent}")
    require_absent_tag(ui_repo, f"v{ui}")

    compatibility_path = ui_repo / "compatibility.json"
    compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
    contract = engine_contract(mira_repo)
    expected = {
        "ui": ui,
        "agent": agent,
        "api_contract": contract,
    }
    mismatches = {
        key: {"expected": value, "actual": compatibility.get(key)}
        for key, value in expected.items()
        if compatibility.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"compatibility mismatch: {json.dumps(mismatches, indent=2)}")

    validator = ui_repo / "scripts/validate-compatibility.mjs"
    run(
        "node",
        str(validator),
        "--file",
        str(compatibility_path),
        "--require-ui",
        ui,
        cwd=ui_repo,
    )

    required = (
        mira_repo / ".github/workflows/agent-release.yml",
        mira_repo / ".github/workflows/release-train.yml",
        ui_repo / ".github/workflows/desktop-release.yml",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"missing release workflows: {missing}")

    print(json.dumps({
        "status": "ok",
        "agent_tag": f"v{agent}",
        "ui_tag": f"v{ui}",
        "api_contract": contract,
        "release_train": compatibility.get("release_train"),
        "min_agent_for_ui": compatibility.get("min_agent_for_ui"),
        "mira_dev": run("git", "rev-parse", "origin/dev", cwd=mira_repo),
        "mira_release": run("git", "rev-parse", "origin/release", cwd=mira_repo),
        "ui_dev": run("git", "rev-parse", "origin/dev", cwd=ui_repo),
        "ui_release": run("git", "rev-parse", "origin/release", cwd=ui_repo),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
