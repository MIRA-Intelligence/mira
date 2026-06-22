"""Tests for the client-side fork-and-PR orchestration (#24)."""

import pytest

from mira_engine.community.contribute import (
    ContributeError,
    OpenPrResult,
    _parse_pr_number,
    _slugify_branch,
    open_pr_github,
)


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


def test_parse_pr_number():
    assert _parse_pr_number("https://github.com/o/r/pull/42") == 42
    assert _parse_pr_number("https://cnb.cool/o/r/-/pulls/7") == 7
    assert _parse_pr_number("https://example.com/no-number") is None


def test_slugify_branch():
    assert _slugify_branch("Fix the Bug!").startswith("mira/fix-the-bug")
    assert _slugify_branch("") == "mira/change"


async def test_open_pr_github_happy_path():
    runner = FakeRunner(
        {
            "gh api user": (0, "alice\n", ""),
            "gh pr create": (0, "https://github.com/o/r/pull/42\n", ""),
        }
    )
    result = await open_pr_github(
        repo="o/r",
        title="Fix bug",
        body="details",
        working_dir="/tmp/repo",
        runner=runner,
    )
    assert isinstance(result, OpenPrResult)
    assert result.number == 42
    assert result.url == "https://github.com/o/r/pull/42"
    assert result.login == "alice"
    assert result.branch.startswith("mira/fix-bug")
    # Pushed to the user's own fork, and the PR head is namespaced to the login.
    assert runner.ran("git switch -c")
    assert runner.ran("git push --force https://github.com/alice/r.git")
    assert runner.ran("--head alice:")


async def test_open_pr_github_uses_provided_login_without_gh_api():
    runner = FakeRunner({"gh pr create": (0, "https://github.com/o/r/pull/9\n", "")})
    result = await open_pr_github(
        repo="o/r", title="t", login="bob", working_dir=".", runner=runner
    )
    assert result.login == "bob"
    assert not runner.ran("gh api user")


async def test_open_pr_github_no_changes_raises():
    runner = FakeRunner(
        {
            "git commit": (1, "", "nothing to commit, working tree clean"),
            "git rev-list": (0, "0\n", ""),
        }
    )
    with pytest.raises(ContributeError, match="No changes to contribute"):
        await open_pr_github(repo="o/r", title="t", login="bob", runner=runner)


async def test_open_pr_github_step_failure_raises():
    runner = FakeRunner({"git switch": (1, "", "fatal: branch exists")})
    with pytest.raises(ContributeError, match="git switch"):
        await open_pr_github(repo="o/r", title="t", login="bob", runner=runner)
