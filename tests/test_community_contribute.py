"""Tests for the bounded client-side fork-and-PR runtime (#24, mira#204)."""

import json

import pytest

from mira_engine.community.contribute import (
    ContributeError,
    ContributionScope,
    OpenPrResult,
    _parse_porcelain_z,
    _parse_pr_number,
    _slugify_branch,
    check_scope,
    load_scope,
    open_pr_github,
    scope_path,
    start_contribution,
)
from mira_engine.community.privacy import PrivacyGuard

BRANCH = "mira/fix-bug-abc123"


class FakeRunner:
    """Records git/gh commands and returns canned results by substring match."""

    def __init__(self, responses=None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    async def __call__(self, cmd, cwd):
        self.calls.append(cmd)
        key = " ".join(cmd)
        for pat, result in self.responses.items():
            if pat in key:
                return result
        return (0, "", "")

    def ran(self, substr: str) -> bool:
        return any(substr in " ".join(c) for c in self.calls)

    def call(self, substr: str) -> list[str]:
        return next(c for c in self.calls if substr in " ".join(c))


def _workspace(tmp_path, **scope_kw):
    ws = tmp_path / "contrib"
    (ws / ".git").mkdir(parents=True)
    scope = {"repo": "o/r", "landing_branch": "contrib", "branch": BRANCH, **scope_kw}
    scope_path(ws).write_text(json.dumps(scope))
    return str(ws)


def _runner(
    status="?? plugins/x/a.py\0 M docs/b.md\0",
    names="plugins/x/a.py\0docs/b.md\0",
    diff="+print('hi')\n",
    **extra,
):
    return FakeRunner(
        {
            "rev-parse --abbrev-ref HEAD": (0, BRANCH + "\n", ""),
            "status --porcelain": (0, status, ""),
            "diff --name-only": (0, names, ""),
            "git diff origin/": (0, diff, ""),
            "gh api user": (0, "alice\n", ""),
            "gh pr create": (0, "https://github.com/o/r/pull/42\n", ""),
            **extra,
        }
    )


def test_parse_pr_number():
    assert _parse_pr_number("https://github.com/o/r/pull/42") == 42
    assert _parse_pr_number("https://cnb.cool/o/r/-/pulls/7") == 7
    assert _parse_pr_number("https://example.com/no-number") is None


def test_slugify_branch():
    assert _slugify_branch("Fix the Bug!").startswith("mira/fix-the-bug")
    assert _slugify_branch("") == "mira/change"


def test_parse_porcelain_keeps_both_ends_of_renames():
    out = "R  new/a.py\0old/a.py\0?? plugins/x.py\0 D gone.py\0"
    assert _parse_porcelain_z(out) == ["new/a.py", "old/a.py", "plugins/x.py", "gone.py"]


def test_check_scope_rules():
    plugin_only = ContributionScope(
        repo="o/r", landing_branch="contrib", branch=BRANCH, allowed_lanes=["plugin"]
    )
    assert check_scope(["plugins/x/a.py", "docs/a.md"], plugin_only) == []
    assert check_scope(["mira_engine/agent/loop.py"], plugin_only)
    both = ContributionScope(repo="o/r", landing_branch="contrib", branch=BRANCH)
    assert check_scope(["mira_engine/agent/loop.py"], both) == []
    assert check_scope(["plugins/x/.env"], both)  # credentials never, in any lane
    assert check_scope(["deploy/server.pem"], both)
    work_order = ContributionScope(
        repo="o/r", landing_branch="contrib", branch=BRANCH, allowed_paths=["plugins/x/**"]
    )
    assert check_scope(["plugins/x/a.py"], work_order) == []
    assert check_scope(["plugins/y/a.py"], work_order)


def test_load_scope_refuses_any_other_directory(tmp_path):
    with pytest.raises(ContributeError, match="not a community contribution workspace"):
        load_scope(tmp_path)


async def test_start_contribution_clones_landing_branch_into_isolated_workspace(tmp_path):
    runner = FakeRunner()
    path, scope = await start_contribution(
        repo="o/r",
        title="Fix bug",
        landing_branch="contrib",
        workspace=tmp_path,
        allowed_lanes=["plugin"],
        runner=runner,
    )
    assert path.is_relative_to(tmp_path / "community-contrib")
    clone = runner.call("git clone")
    assert clone[clone.index("--branch") + 1] == "contrib"
    assert runner.ran(f"git switch -c {scope.branch}")
    assert scope.branch.startswith("mira/fix-bug-")
    assert load_scope(path).allowed_lanes == ["plugin"]


async def test_start_contribution_rejects_bad_repo(tmp_path):
    with pytest.raises(ContributeError):
        await start_contribution(
            repo="o/r; rm -rf /",
            title="x",
            landing_branch="contrib",
            workspace=tmp_path,
            runner=FakeRunner(),
        )


async def test_open_pr_happy_path_stages_exactly_and_targets_landing_branch(tmp_path):
    runner = _runner()
    result = await open_pr_github(
        working_dir=_workspace(tmp_path), title="Fix bug", body="details", runner=runner
    )
    assert isinstance(result, OpenPrResult)
    assert (result.number, result.login, result.branch) == (42, "alice", BRANCH)
    assert runner.call("git add") == ["git", "add", "--all", "--", "docs/b.md", "plugins/x/a.py"]
    push = runner.call("git push")
    assert "--force" not in " ".join(push) and push[-1] == f"{BRANCH}:{BRANCH}"
    pr = runner.call("gh pr create")
    assert pr[pr.index("--base") + 1] == "contrib"
    assert pr[pr.index("--head") + 1] == f"alice:{BRANCH}"


async def test_open_pr_uses_provided_login_without_gh_api(tmp_path):
    runner = _runner()
    result = await open_pr_github(
        working_dir=_workspace(tmp_path), title="t", login="bob", runner=runner
    )
    assert result.login == "bob"
    assert not runner.ran("gh api user")


async def test_open_pr_refuses_outside_a_contribution_workspace(tmp_path):
    runner = _runner()
    with pytest.raises(ContributeError, match="not a community contribution workspace"):
        await open_pr_github(working_dir=str(tmp_path), title="t", runner=runner)
    assert runner.calls == []


async def test_open_pr_refuses_out_of_scope_changes_before_staging(tmp_path):
    runner = _runner(status="?? mira_engine/agent/loop.py\0")
    with pytest.raises(ContributeError, match="refusing to stage"):
        await open_pr_github(
            working_dir=_workspace(tmp_path, allowed_lanes=["plugin"]), title="t", runner=runner
        )
    assert not runner.ran("git add") and not runner.ran("git push")


async def test_open_pr_refuses_credential_files(tmp_path):
    runner = _runner(status="?? plugins/x/.env\0")
    with pytest.raises(ContributeError, match="credential-like"):
        await open_pr_github(working_dir=_workspace(tmp_path), title="t", runner=runner)
    assert not runner.ran("git add")


async def test_open_pr_rechecks_commits_the_agent_made_itself(tmp_path):
    # Nothing uncommitted, but the branch already carries an out-of-scope commit.
    runner = _runner(status="", names="plugins/x/a.py\0.github/workflows/tests.yml\0")
    with pytest.raises(ContributeError, match="refusing to push"):
        await open_pr_github(
            working_dir=_workspace(tmp_path, allowed_lanes=["plugin"]), title="t", runner=runner
        )
    assert not runner.ran("git push")


async def test_open_pr_refuses_diffs_with_credentials(tmp_path):
    runner = _runner(diff="+TOKEN = 'ghp_" + "a" * 36 + "'\n")
    with pytest.raises(ContributeError, match="credential"):
        await open_pr_github(
            working_dir=_workspace(tmp_path), title="t", guard=PrivacyGuard(), runner=runner
        )
    assert not runner.ran("git push")


async def test_open_pr_requires_the_contribution_branch(tmp_path):
    runner = _runner(**{"rev-parse --abbrev-ref HEAD": (0, "main\n", "")})
    with pytest.raises(ContributeError, match="contribution branch"):
        await open_pr_github(working_dir=_workspace(tmp_path), title="t", runner=runner)


async def test_open_pr_refuses_a_different_repo(tmp_path):
    with pytest.raises(ContributeError, match="contributes to o/r"):
        await open_pr_github(
            working_dir=_workspace(tmp_path), title="t", repo="evil/r", runner=_runner()
        )


async def test_open_pr_no_changes_raises(tmp_path):
    with pytest.raises(ContributeError, match="No changes"):
        await open_pr_github(
            working_dir=_workspace(tmp_path), title="t", runner=_runner(status="", names="")
        )


async def test_open_pr_step_failure_raises(tmp_path):
    runner = _runner(**{"git push": (1, "", "permission denied")})
    with pytest.raises(ContributeError, match="permission denied"):
        await open_pr_github(
            working_dir=_workspace(tmp_path), title="t", login="alice", runner=runner
        )


# --------------------------------------------------------------------------- #
# The default runner: `gh` on a service PATH                                   #
# --------------------------------------------------------------------------- #


async def test_default_runner_reports_a_missing_program_as_a_contribute_error(monkeypatch):
    # The desktop bundle's gateway runs under launchd with the bare system
    # PATH; `gh` from Homebrew raised FileNotFoundError out of the tool.
    import mira_engine.community.contribute as mod

    monkeypatch.setattr(mod, "resolve_executable", lambda name, *a, **k: None)
    with pytest.raises(ContributeError, match="`gh` is not installed") as info:
        await mod._default_runner(["gh", "auth", "status"], None)
    assert "background service" in str(info.value)


async def test_default_runner_runs_the_resolved_program_with_the_extended_path(
    monkeypatch, tmp_path
):
    import sys

    import mira_engine.community.contribute as mod

    seen: dict = {}

    def fake_resolve(name, *a, **k):
        seen["name"] = name
        return sys.executable

    monkeypatch.setattr(mod, "resolve_executable", fake_resolve)
    monkeypatch.setattr(
        mod,
        "extended_environ",
        lambda *a, **k: {**__import__("os").environ, "MIRA_RUNNER_PROBE": "1"},
    )

    code, out, err = await mod._default_runner(
        ["gh", "-c", "import os, sys; print(os.environ['MIRA_RUNNER_PROBE'], file=sys.stdout)"],
        str(tmp_path),
    )

    assert seen["name"] == "gh"
    assert (code, out.strip(), err) == (0, "1", "")


async def test_start_contribution_surfaces_the_missing_gh_before_cloning(tmp_path, monkeypatch):
    import mira_engine.community.contribute as mod

    monkeypatch.setattr(mod, "resolve_executable", lambda name, *a, **k: None)
    with pytest.raises(ContributeError, match="`gh` is not installed"):
        await start_contribution(
            repo="o/r", title="t", landing_branch="contrib", workspace=tmp_path
        )
    assert not (tmp_path / "community-contrib").exists()
