"""Verify published engine/UI releases and their exact compatibility mapping."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from pathlib import Path


def version_from_tag(tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:rc\d+)?", tag):
        raise ValueError(f"invalid release tag: {tag}")
    return tag[1:]


def validate_pair(agent: dict, ui: dict, compatibility: dict, agent_tag: str, ui_tag: str) -> dict:
    agent_version, ui_version = version_from_tag(agent_tag), version_from_tag(ui_tag)
    for release, tag in ((agent, agent_tag), (ui, ui_tag)):
        if release.get("draft") or not release.get("published_at") or release.get("tag_name") != tag:
            raise ValueError(f"release is not published at the requested tag: {tag}")
        if bool(release.get("prerelease")) != ("rc" in tag):
            raise ValueError(f"incorrect prerelease flag: {tag}")
    expected = {"agent": agent_version, "ui": ui_version,
                "min_agent_for_ui": agent_version, "api_contract": "v1"}
    for key, value in expected.items():
        if compatibility.get(key) != value:
            raise ValueError(f"compatibility mismatch for {key}: expected {value}")
    if not re.fullmatch(r"\d{4}\.\d{2}(?:rc\d+)?", compatibility.get("release_train", "")):
        raise ValueError("invalid release_train")
    engine_assets = {a["name"] for a in agent.get("assets", []) if a.get("size", 0) > 0}
    desktop_assets = {a["name"] for a in ui.get("assets", []) if a.get("size", 0) > 0}
    required_engine = {"SHA256SUMS.txt", "mira-engine-linux-x86_64", "mira-engine-windows-x86_64.zip"}
    if not required_engine <= engine_assets or not any(n.startswith("mira-engine-macos-") for n in engine_assets):
        raise ValueError("missing engine executable/checksum assets")
    if not any(n.startswith(f"mira_engine-{agent_version}-") and n.endswith(".whl") for n in engine_assets):
        raise ValueError("missing matching engine wheel")
    if f"mira_engine-{agent_version}.tar.gz" not in engine_assets:
        raise ValueError("missing matching engine sdist")
    desktop_version = re.sub(r"rc(\d+)$", r"-rc.\1", ui_version)
    channel = "rc" if "rc" in ui_version else "latest"
    for kind in ("standalone", "bundle"):
        prefix = f"MIRA-{kind}-{desktop_version}-"
        for platform, suffix in (("mac", ".dmg"), ("mac", ".zip"), ("win", "-setup.zip"), ("win", "-setup.exe")):
            if not any(n.startswith(prefix + platform + "-") and n.endswith(suffix) for n in desktop_assets):
                raise ValueError(f"missing {kind} {platform} {suffix} installer")
        for platform in ("mac", "win"):
            if f"{channel}-{platform}-{kind}.yml" not in desktop_assets:
                raise ValueError(f"missing {kind} {platform} update manifest")
    return {"ok": True, "agent_tag": agent_tag, "ui_tag": ui_tag,
            "compatibility": compatibility, "engine_assets": sorted(engine_assets),
            "ui_assets": sorted(desktop_assets)}


def gh_json(endpoint: str) -> dict:
    return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True))


def tag_commit(repo: str, tag: str) -> str:
    obj = gh_json(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    while obj["type"] == "tag":
        obj = gh_json(f"repos/{repo}/git/tags/{obj['sha']}")["object"]
    if obj["type"] != "commit":
        raise ValueError(f"{tag} does not resolve to a commit")
    return obj["sha"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-tag", required=True)
    parser.add_argument("--ui-tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    version_from_tag(args.agent_tag)
    version_from_tag(args.ui_tag)
    agent_repo, ui_repo = "MIRA-Intelligence/mira", "MIRA-Intelligence/mira-ui"
    agent = gh_json(f"repos/{agent_repo}/releases/tags/{args.agent_tag}")
    ui = gh_json(f"repos/{ui_repo}/releases/tags/{args.ui_tag}")
    content = gh_json(f"repos/{ui_repo}/contents/compatibility.json?ref={args.ui_tag}")
    compatibility = json.loads(base64.b64decode(content["content"]))
    report = validate_pair(agent, ui, compatibility, args.agent_tag, args.ui_tag)
    report.update(agent_commit=tag_commit(agent_repo, args.agent_tag), ui_commit=tag_commit(ui_repo, args.ui_tag))
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
