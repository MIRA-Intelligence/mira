"""Client-side fork-and-PR: open a real PR on an upstream repo (#24).

The direct fork-and-PR contribution model keeps code off the community
platform entirely. Instead of uploading a diff, the engine commits the agent's
working changes, pushes them to the *user's own fork*, and opens a PR upstream
using the **user's local git credentials** (via the ``gh`` CLI). The community
platform only ever sees the registered PR metadata, never a token or a diff.

The git/gh steps are funnelled through an injectable command runner so the
orchestration is unit-testable without touching the network or a real repo.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# (returncode, stdout, stderr)
CommandRunner = Callable[[list[str], str | None], Awaitable[tuple[int, str, str]]]


class ContributeError(Exception):
    """Raised when a local git/gh step of opening a PR fails."""


@dataclass
class OpenPrResult:
    url: str
    number: int
    branch: str
    login: str


async def _default_runner(cmd: list[str], cwd: str | None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _parse_pr_number(url: str) -> int | None:
    """Extract the PR number from a GitHub/CNB PR URL (.../pull[s]/<n>)."""
    m = re.search(r"/pulls?/(\d+)", url)
    return int(m.group(1)) if m else None


def _slugify_branch(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"
    return f"mira/{slug}"


async def open_pr_github(
    *,
    repo: str,
    title: str,
    body: str = "",
    base_ref: str = "main",
    branch: str | None = None,
    working_dir: str = ".",
    login: str | None = None,
    runner: CommandRunner | None = None,
) -> OpenPrResult:
    """Commit working changes, push to the user's fork, and open a PR upstream.

    Uses ``gh`` so the user's existing GitHub auth/credentials are reused; no
    token is ever sent to the community platform. Returns the opened PR.
    """
    run = runner or _default_runner
    branch = branch or _slugify_branch(title)
    name = repo.split("/", 1)[1] if "/" in repo else repo

    async def step(cmd: list[str], *, allow_fail: bool = False) -> tuple[int, str, str]:
        code, out, err = await run(cmd, working_dir)
        if code != 0 and not allow_fail:
            raise ContributeError(
                f"`{shlex.join(cmd)}` failed ({code}): {(err or out).strip()[:400]}"
            )
        return code, out, err

    # 1. Create the head branch from the current checkout.
    await step(["git", "switch", "-c", branch])

    # 2. Commit the agent's working changes (if any are uncommitted yet).
    await step(["git", "add", "-A"])
    code, out, err = await step(["git", "commit", "-m", title], allow_fail=True)
    if code != 0 and "nothing to commit" in (out + err).lower():
        ahead, ah_out, _ = await step(
            ["git", "rev-list", "--count", f"{base_ref}..HEAD"], allow_fail=True
        )
        if ahead != 0 or ah_out.strip() in ("", "0"):
            raise ContributeError("No changes to contribute: commit or stage your edits first.")
    elif code != 0:
        raise ContributeError(f"git commit failed: {(err or out).strip()[:400]}")

    # 3. Resolve the user's GitHub login (the PR author).
    if not login:
        _, who, _ = await step(["gh", "api", "user", "-q", ".login"])
        login = who.strip()
    if not login:
        raise ContributeError("could not determine your GitHub login (is `gh` authenticated?)")

    # 4. Ensure a fork exists, then push the branch to it.
    await step(["gh", "repo", "fork", repo, "--clone=false"], allow_fail=True)
    fork_url = f"https://github.com/{login}/{name}.git"
    await step(["git", "push", "--force", fork_url, f"{branch}:{branch}"])

    # 5. Open the PR upstream from the fork branch.
    _, pr_out, _ = await step(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base_ref,
            "--head",
            f"{login}:{branch}",
            "--title",
            title,
            "--body",
            body or title,
        ]
    )
    url = next((ln.strip() for ln in pr_out.splitlines() if ln.strip().startswith("http")), "")
    if not url:
        raise ContributeError(f"could not parse PR URL from gh output: {pr_out.strip()[:200]}")
    number = _parse_pr_number(url)
    if number is None:
        raise ContributeError(f"could not parse PR number from URL: {url}")
    return OpenPrResult(url=url, number=number, branch=branch, login=login)
