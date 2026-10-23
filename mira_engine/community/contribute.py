"""Client-side fork-and-PR inside a bounded contribution workspace (#24, mira#204).

The direct fork-and-PR model keeps code off the community platform: the engine
commits the agent's changes, pushes them to the *user's own fork* and opens a
PR upstream with the **user's local git credentials** (``gh``). The platform
only sees PR metadata.

Because this runs on the user's machine with the user's credentials, it is the
place where the user-side boundary is enforced (design §5.1, layer L2):

* **Isolated workspace** — contributions happen in a fresh clone under
  ``<workspace>/community-contrib/``, created by :func:`start_contribution`.
  A PR is never opened from any other directory, so nothing from the user's
  own projects can be committed.
* **Scope** — every changed path is checked against the contribution scope
  (work-order ``allowed_paths`` when set, else the lanes the user allows) and
  against credential-looking filenames before anything is staged.
* **Exact staging** — only the validated paths are staged (never ``git add -A``).
* **Credential scan** — the full diff against the landing branch is scanned
  before it is pushed.
* **Push discipline** — only the workspace's own ``mira/…`` branch is pushed,
  to the user's fork, never forced, and the PR always targets the landing branch.

The platform re-checks the change from the host's side (L3); this layer exists
so a misbehaving agent cannot leak or push something in the first place.

The git/gh steps go through an injectable command runner so the orchestration
is unit-testable without touching the network or a real repo.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mira_engine.community.privacy import PrivacyGuard
from mira_engine.utils.exec_path import (
    extended_environ,
    missing_executable_message,
    resolve_executable,
)

# (returncode, stdout, stderr)
CommandRunner = Callable[[list[str], str | None], Awaitable[tuple[int, str, str]]]

CONTRIB_DIRNAME = "community-contrib"
# Lives inside .git/, so it is never committed.
SCOPE_FILENAME = "mira-contribution.json"
BRANCH_PREFIX = "mira/"
DEFAULT_PLUGIN_LANE_PATHS = ("plugins/**", "docs/**")

# Filenames that almost always hold credentials; never part of a contribution.
_CREDENTIAL_FILES = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.pem|.*\.p12|.*\.pfx|.*\.key|id_rsa.*|id_ed25519.*"
    r"|\.netrc|\.npmrc|\.pypirc|credentials(\.json)?)$",
    re.IGNORECASE,
)


class ContributeError(Exception):
    """Raised when a local git/gh step of a contribution fails or is refused."""


@dataclass
class OpenPrResult:
    url: str
    number: int
    branch: str
    login: str
    # True when the branch already had an open PR: the push updated it.
    updated: bool = False
    # Every path the branch changes against the landing branch.
    changed_paths: list[str] = field(default_factory=list)


@dataclass
class ContributionScope:
    """What a contribution workspace may change; persisted in ``.git/``."""

    repo: str
    landing_branch: str
    branch: str
    plugin_lane_paths: list[str] = field(default_factory=lambda: list(DEFAULT_PLUGIN_LANE_PATHS))
    allowed_lanes: list[str] = field(default_factory=lambda: ["plugin", "core"])
    # Work-order scope (P2); when set it replaces the lane rule.
    allowed_paths: list[str] | None = None
    work_order_id: str | None = None
    # The development post this contribution implements; the PR links to it.
    proposal_id: str | None = None


async def _default_runner(cmd: list[str], cwd: str | None) -> tuple[int, str, str]:
    # The gateway may run as a service with the system's bare PATH, where
    # `gh` installed by Homebrew/winget is invisible although the user's
    # terminal finds it. Search the usual install directories too, and turn
    # "not installed" into a ContributeError the agent can relay instead of
    # a FileNotFoundError traceback.
    program = resolve_executable(cmd[0])
    if program is None:
        raise ContributeError(missing_executable_message(cmd[0]))
    try:
        proc = await asyncio.create_subprocess_exec(
            program,
            *cmd[1:],
            cwd=cwd,
            env=extended_environ(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise ContributeError(f"could not run `{shlex.join(cmd)}`: {e}") from e
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _parse_pr_number(url: str) -> int | None:
    """Extract the PR number from a GitHub/CNB PR URL (.../pull[s]/<n>)."""
    m = re.search(r"/pulls?/(\d+)", url)
    return int(m.group(1)) if m else None


def _slugify_branch(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"
    return f"{BRANCH_PREFIX}{slug}"


# --------------------------------------------------------------------------- #
# Scope                                                                        #
# --------------------------------------------------------------------------- #


def _glob_to_re(glob: str) -> re.Pattern[str]:
    """``**`` any depth, ``*`` within a segment, ``?`` one char (as on the platform)."""
    out, i = "", 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            if glob[i + 1 : i + 2] == "*":
                slash = glob[i + 2 : i + 3] == "/"
                out += "(?:.*/)?" if slash else ".*"
                i += 3 if slash else 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile(f"^{out}$")


def _matches(path: str, globs: list[str] | tuple[str, ...]) -> bool:
    return any(_glob_to_re(g).match(path) for g in globs)


def check_scope(paths: list[str], scope: ContributionScope) -> list[str]:
    """Human-readable problems with ``paths`` under ``scope`` (empty = ok)."""
    problems: list[str] = []
    creds = [p for p in paths if _CREDENTIAL_FILES.search(p)]
    if creds:
        problems.append(f"credential-like files must never be contributed: {', '.join(creds)}")
    if scope.allowed_paths is not None:
        outside = [p for p in paths if not _matches(p, scope.allowed_paths)]
        if outside:
            problems.append(
                f"outside the work order's allowed paths ({', '.join(scope.allowed_paths)}): "
                f"{', '.join(outside)}"
            )
    elif "core" not in scope.allowed_lanes:
        core = [p for p in paths if not _matches(p, scope.plugin_lane_paths)]
        if core:
            problems.append(
                "your user only allows plugin-lane contributions "
                f"({', '.join(scope.plugin_lane_paths)}); these are core paths: {', '.join(core)}"
            )
    return problems


def scope_path(working_dir: str | Path) -> Path:
    return Path(working_dir) / ".git" / SCOPE_FILENAME


def load_scope(working_dir: str | Path) -> ContributionScope:
    """The scope of a contribution workspace; refuses any other directory."""
    try:
        data = json.loads(scope_path(working_dir).read_text(encoding="utf-8"))
        return ContributionScope(**data)
    except (OSError, ValueError, TypeError) as e:
        raise ContributeError(
            f"'{working_dir}' is not a community contribution workspace. Start one with "
            "community_start_contribution and make your changes there; PRs are never opened "
            "from your user's own directories."
        ) from e


def contributions_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser() / CONTRIB_DIRNAME


def describe_scope(scope: ContributionScope) -> dict[str, Any]:
    """Compact scope summary for tool output."""
    return {
        "repo": scope.repo,
        "landing_branch": scope.landing_branch,
        "branch": scope.branch,
        "allowed_paths": scope.allowed_paths,
        "allowed_lanes": scope.allowed_lanes,
        "plugin_lane_paths": scope.plugin_lane_paths,
        "proposal_id": scope.proposal_id,
    }


# --------------------------------------------------------------------------- #
# Workflow                                                                     #
# --------------------------------------------------------------------------- #


def _stepper(run: CommandRunner, cwd: str | None):
    async def step(cmd: list[str], *, allow_fail: bool = False) -> tuple[int, str, str]:
        code, out, err = await run(cmd, cwd)
        if code != 0 and not allow_fail:
            raise ContributeError(f"`{shlex.join(cmd)}` failed ({code}): {(err or out).strip()[:400]}")
        return code, out, err

    return step


async def start_contribution(
    *,
    repo: str,
    title: str,
    landing_branch: str,
    workspace: str | Path,
    plugin_lane_paths: list[str] | None = None,
    allowed_lanes: list[str] | None = None,
    allowed_paths: list[str] | None = None,
    work_order_id: str | None = None,
    proposal_id: str | None = None,
    host_url: str = "https://github.com",
    runner: CommandRunner | None = None,
) -> tuple[Path, ContributionScope]:
    """Clone ``repo`` at the landing branch into a fresh, isolated workspace and
    check out the contribution branch. Returns the workspace and its scope."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ContributeError(f"repo must be 'owner/name', got {repo!r}")
    if not landing_branch:
        raise ContributeError("the community did not report a landing branch")
    run = runner or _default_runner
    owner, name = repo.split("/", 1)
    branch = f"{_slugify_branch(title)}-{uuid.uuid4().hex[:6]}"
    target = contributions_root(workspace) / f"{owner}__{name}" / branch.removeprefix(BRANCH_PREFIX)

    # The PR is pushed with the user's own GitHub credentials: find out now,
    # not after the change is written, that `gh` isn't logged in.
    code, _, err = await run(["gh", "auth", "status"], None)
    if code != 0:
        raise ContributeError(
            "GitHub CLI is not logged in (`gh auth status` failed"
            + (f": {err.strip()[:200]}" if err.strip() else "")
            + "). Your user needs to run `gh auth login` first; the PR is opened from their fork "
            "with their credentials."
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    await _stepper(run, None)(
        ["git", "clone", "--filter=blob:none", "--branch", landing_branch, f"{host_url}/{repo}.git", str(target)]
    )
    await _stepper(run, str(target))(["git", "switch", "-c", branch])

    scope = ContributionScope(
        repo=repo,
        landing_branch=landing_branch,
        branch=branch,
        plugin_lane_paths=list(plugin_lane_paths or DEFAULT_PLUGIN_LANE_PATHS),
        allowed_lanes=list(allowed_lanes or ["plugin", "core"]),
        allowed_paths=list(allowed_paths) if allowed_paths is not None else None,
        work_order_id=work_order_id,
        proposal_id=proposal_id,
    )
    path = scope_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(scope), indent=2), encoding="utf-8")
    return target, scope


def _parse_porcelain_z(out: str) -> list[str]:
    """Paths from ``git status --porcelain=v1 -z`` (both ends of renames)."""
    paths: list[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            if i < len(entries) and entries[i]:
                paths.append(entries[i])
            i += 1
    return paths


async def open_pr_github(
    *,
    working_dir: str,
    title: str,
    body: str = "",
    repo: str | None = None,
    login: str | None = None,
    guard: PrivacyGuard | None = None,
    runner: CommandRunner | None = None,
) -> OpenPrResult:
    """Commit the workspace's in-scope changes, push its branch to the user's
    fork and open a PR into the landing branch. Refuses anything outside the
    contribution workspace, outside its scope, or carrying credentials."""
    scope = load_scope(working_dir)
    if repo and repo.lower() != scope.repo.lower():
        raise ContributeError(f"this workspace contributes to {scope.repo}, not {repo}")
    step = _stepper(runner or _default_runner, working_dir)
    guard = guard or PrivacyGuard()

    # 1. Stay on the workspace's own branch.
    _, current, _ = await step(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if current.strip() != scope.branch:
        raise ContributeError(
            f"the workspace must stay on its contribution branch '{scope.branch}' "
            f"(now on '{current.strip()}')"
        )

    # 2. Validate, then stage exactly the uncommitted changes.
    _, status, _ = await step(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    pending = _parse_porcelain_z(status)
    problems = check_scope(pending, scope)
    if problems:
        raise ContributeError("refusing to stage: " + "; ".join(problems))
    if pending:
        await step(["git", "add", "--all", "--", *sorted(set(pending))])
        await step(["git", "commit", "-m", title])

    # 3. Re-check everything the branch adds (covers the agent's own commits).
    base = f"origin/{scope.landing_branch}"
    _, names, _ = await step(["git", "diff", "--name-only", "-z", f"{base}...HEAD"])
    changed = [p for p in names.split("\0") if p]
    if not changed:
        raise ContributeError("No changes to contribute: edit files in the contribution workspace first.")
    problems = check_scope(changed, scope)
    if problems:
        raise ContributeError("refusing to push: " + "; ".join(problems))
    _, diff, _ = await step(["git", "diff", f"{base}...HEAD"])
    secrets = guard.scan(diff, secrets_only=True)
    if secrets:
        kinds = ", ".join(sorted({f.label for f in secrets}))
        raise ContributeError(f"refusing to push: the change contains a credential ({kinds}); remove it first")

    # 4. The PR author is the user's GitHub login.
    if not login:
        _, who, _ = await step(["gh", "api", "user", "-q", ".login"])
        login = who.strip()
    if not login:
        raise ContributeError("could not determine your GitHub login (is `gh` authenticated?)")

    # 5. Ensure the fork exists; push only this workspace's branch to it. No
    #    force: follow-up commits fast-forward, history rewrites are refused.
    name = scope.repo.split("/", 1)[1]
    await step(["gh", "repo", "fork", scope.repo, "--clone=false"], allow_fail=True)
    fork_url = f"https://github.com/{login}/{name}.git"
    await step(["git", "push", fork_url, f"{scope.branch}:{scope.branch}"])

    # 6. Open the PR into the landing branch — unless this branch already has
    #    an open PR (follow-up commits after a review): the push updated it.
    existing = await _existing_pr(step, scope, login)
    if existing is not None:
        url, number = existing
        return OpenPrResult(url=url, number=number, branch=scope.branch, login=login, updated=True, changed_paths=changed)
    _, pr_out, _ = await step(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            scope.repo,
            "--base",
            scope.landing_branch,
            "--head",
            f"{login}:{scope.branch}",
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
    return OpenPrResult(url=url, number=number, branch=scope.branch, login=login, changed_paths=changed)


async def _existing_pr(step: Any, scope: ContributionScope, login: str) -> tuple[str, int] | None:
    """The open PR for this workspace's branch on the user's fork, if any."""
    code, out, _ = await step(
        ["gh", "pr", "list", "--repo", scope.repo, "--head", f"{login}:{scope.branch}", "--state", "open", "--json", "number,url"],
        allow_fail=True,
    )
    if code != 0:
        return None
    try:
        prs = json.loads(out or "[]")
    except ValueError:
        return None
    for pr in prs if isinstance(prs, list) else []:
        if isinstance(pr, dict) and pr.get("url") and pr.get("number") is not None:
            return str(pr["url"]), int(pr["number"])
    return None


def plugin_ids_from_paths(paths: list[str]) -> list[str]:
    """The plugins a change adds or edits (``plugins/<id>/...``)."""
    ids = {p.split("/", 2)[1] for p in paths if p.startswith("plugins/") and p.count("/") >= 2}
    return sorted(i for i in ids if i)


async def open_and_register_pr(
    client: Any,
    *,
    working_dir: str,
    title: str,
    body: str = "",
    proposal_id: str | None = None,
    guard: PrivacyGuard | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Open (or update) the PR for a contribution workspace and register it with
    the community — the one code path behind the ``community_open_pr`` tool and
    an approved held action (#72). Raises :class:`ContributeError` when the PR
    can't be opened; a registration failure is reported in the result instead,
    since the PR is open by then."""
    result = await open_pr_github(working_dir=working_dir, title=title, body=body, guard=guard, runner=runner)
    scope = load_scope(working_dir)
    out: dict[str, Any] = {
        "url": result.url,
        "number": result.number,
        "login": result.login,
        "updated": result.updated,
        "repo": scope.repo,
        "proposal_id": proposal_id or scope.proposal_id,
        "work_order_id": scope.work_order_id,
        "plugin_ids": plugin_ids_from_paths(result.changed_paths),
        "pr_id": None,
        "registered": False,
        "error": "",
    }
    if result.updated:
        # Registered when it was opened; the platform re-reads the new head.
        out["registered"] = True
        return out
    try:
        reg = await client.register_pr(
            out["proposal_id"],
            scope.repo,
            result.number,
            result.url,
            result.login,
            host="github",
            title=title,
            body=body,
            work_order_id=scope.work_order_id,
        )
    except Exception as e:  # noqa: BLE001 - the PR is open even if registering fails
        out["error"] = str(e)
        return out
    pr = (reg or {}).get("pr") or {}
    out["registered"] = True
    out["pr_id"] = str(pr.get("id") or "") or None
    out["state"] = pr.get("state") or "open"
    return out


def pr_summary(out: dict[str, Any]) -> str:
    """One paragraph for the agent (or the approvals log) about an opened PR."""
    if out.get("updated"):
        return (
            f"Pushed the follow-up commits to the open PR {out['url']}; the community re-reads its new head "
            "(reviews and approvals of the old head no longer count)."
        )
    if not out.get("registered"):
        return (
            f"Opened PR {out['url']}, but registering it with the community failed: {out.get('error')}. "
            "You can retry registration later."
        )
    return (
        f"Opened and registered PR {out['url']} (id {out.get('pr_id') or '?'}, state {out.get('state', 'open')}). "
        "It merges once required checks pass and the gate for its lane is met "
        "(plugin lane: reviews of the current head; core lane: a maintainer)."
    )
