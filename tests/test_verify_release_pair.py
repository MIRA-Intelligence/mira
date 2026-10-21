from pathlib import Path

import pytest

from scripts.verify_release_pair import validate_pair, version_from_tag


def pair():
    def release(tag, names):
        return {"tag_name": tag, "draft": False, "prerelease": True, "published_at": "2026-10-12",
                "assets": [{"name": n, "size": 100} for n in names]}
    engine = release("v0.3.5rc1", ["SHA256SUMS.txt", "mira-engine-linux-x86_64", "mira-engine-macos-arm64",
        "mira-engine-windows-x86_64.zip", "mira_engine-0.3.5rc1-py3-none-any.whl", "mira_engine-0.3.5rc1.tar.gz"])
    names = []
    for kind in ("bundle", "standalone"):
        names += [f"MIRA-{kind}-0.4.5-rc.1-mac-arm64.dmg", f"MIRA-{kind}-0.4.5-rc.1-win-x64-setup.zip"]
    ui = release("v0.4.5rc1", names)
    compat = {"release_train": "2026.10", "ui": "0.4.5rc1", "agent": "0.3.5rc1",
              "min_agent_for_ui": "0.3.5rc1", "api_contract": "v1"}
    return engine, ui, compat


def test_complete_pair_passes():
    assert validate_pair(*pair(), "v0.3.5rc1", "v0.4.5rc1")["ok"]


@pytest.mark.parametrize("failure", ["draft", "prerelease", "published_at", "missing_asset", "mapping"])
def test_incomplete_or_mismatched_pair_fails(failure):
    engine, ui, compat = pair()
    if failure == "draft":
        engine["draft"] = True
    elif failure in {"prerelease", "published_at"}:
        engine[failure] = False
    elif failure == "missing_asset":
        ui["assets"].pop()
    else:
        compat["agent"] = "0.3.4"
    with pytest.raises(ValueError):
        validate_pair(engine, ui, compat, "v0.3.5rc1", "v0.4.5rc1")


@pytest.mark.parametrize("dropped", ["mac-arm64.dmg", "win-x64-setup.zip"])
def test_each_platform_installer_is_required(dropped):
    engine, ui, compat = pair()
    ui["assets"] = [a for a in ui["assets"] if not a["name"].endswith(dropped)]
    with pytest.raises(ValueError, match="installer"):
        validate_pair(engine, ui, compat, "v0.3.5rc1", "v0.4.5rc1")


def test_tag_input_is_not_shell_code():
    with pytest.raises(ValueError):
        version_from_tag("v0.3.5rc1; echo bad")


def test_release_train_resolves_runner_paths_at_step_runtime():
    workflow = (Path(__file__).parents[1] / ".github/workflows/release-train.yml").read_text()
    assert "${{ runner.temp }}" not in workflow
    assert 'MIRA_HOME=$RUNNER_TEMP/mira-release-train' in workflow
    assert 'MIRA_CONFIG_PATH=$RUNNER_TEMP/mira-release-train/config.json' in workflow
    assert '>> "$GITHUB_ENV"' in workflow
