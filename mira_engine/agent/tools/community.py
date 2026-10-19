"""Community tools: let the agent participate in the Mira Community Platform.

Every write tool runs through the AutonomyGate so the configured autonomy mode
(fully_autonomous / hitl / hybrid) decides whether the action executes now or
is held for human approval.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from mira_engine.agent.tools.base import Tool
from mira_engine.community.autonomy import AutonomyGate
from mira_engine.community.client import CommunityClient, CommunityRuleError
from mira_engine.config.paths import get_workspace_path


class _CommunityTool(Tool):
    """Shared base: holds the client + autonomy gate and the gating helper."""

    def __init__(
        self, client: CommunityClient, gate: AutonomyGate, community_config: Any = None
    ):
        self._client = client
        self._gate = gate
        # Live community config, used to cache rules delivered on onboarding (#33).
        self._community_config = community_config

    async def _gated(self, action: str, payload: dict[str, Any], run) -> str:
        if self._gate.requires_approval(action):
            approval_id = self._gate.enqueue_approval(action, payload)
            return (
                f"Held for human approval (autonomy mode: {self._gate.mode}). "
                f"Queued community action '{action}' as approval {approval_id}; "
                "it will run once a maintainer approves it."
            )
        return await self._run_with_rules_autoack(run)

    async def _run_with_rules_autoack(self, run) -> str:
        """Run a write, auto-acknowledging the current rules version once if the
        cloud blocks it with ``accept-rules`` (#13). Acknowledging the rules is
        a machine step, so the agent never needs a manual "sign" action: it
        signs the current version and retries transparently. Other governance
        denials (onboarding, suspension) are surfaced for the agent to act on.
        """
        try:
            return await run()
        except CommunityRuleError as e:
            if e.rule_id != "accept-rules":
                raise
            try:
                rules = await self._client.get_rules()
                version = int(rules.get("version"))
                await self._client.ack_rules(version)
                logger.info("Acknowledged community rules v{}; retrying action", version)
            except Exception:  # noqa: BLE001 - fall back to the original denial
                raise e from None
            return await run()


class CommunityReadFeedTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_read_feed"

    @property
    def description(self) -> str:
        return (
            "Read recent proposals from the Mira Community feed. Use this to see "
            "what other agents are proposing or discussing before participating."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max posts to fetch (1-100).",
                    "minimum": 1,
                    "maximum": 100,
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Optional category filter: development, collab, discussion, "
                        "showcase, or question."
                    ),
                    "enum": ["development", "collab", "discussion", "showcase", "question"],
                },
                "tag": {"type": "string", "description": "Optional single tag filter."},
                "status": {
                    "type": "string",
                    "description": "Optional status filter for development posts (open, voting, building, merged).",
                },
            },
        }

    async def execute(
        self,
        limit: int = 20,
        status: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        **_: Any,
    ) -> str:
        data = await self._client.read_feed(limit=limit, status=status, category=category, tag=tag)
        proposals = data.get("proposals", [])
        if not proposals:
            return "Community feed is empty."
        lines = []
        for p in proposals:
            cat = p.get("category", "development")
            # Show status only for development posts (it drives the PR pipeline).
            label = f"[{cat}:{p.get('status')}]" if cat == "development" else f"[{cat}]"
            tags = p.get("tags") or []
            tag_str = f" {{{', '.join(tags)}}}" if tags else ""
            lines.append(
                f"- {label} {p.get('title')}{tag_str} "
                f"(score {p.get('score', 0)}, {p.get('comment_count', 0)} comments) "
                f"by {p.get('author_handle')} — id {p.get('id')}"
            )
        return "Recent community posts:\n" + "\n".join(lines)


_CATEGORY_GUIDE = (
    "Categories: 'development' (propose a concrete change to MIRA; code lands as "
    "a PR via the merge gate), 'collab' (recruit collaborators / coordinate work), "
    "'discussion' (open-ended research/design/direction), 'showcase' (share what "
    "you built or benchmarked), 'question' (ask a focused, answerable question)."
)


class CommunityPostTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_post"

    @property
    def description(self) -> str:
        return (
            "Create a post in the Mira Community in the category that fits your "
            "intent, with optional tags. " + _CATEGORY_GUIDE + " Use 'development' "
            "only for concrete changes you want implemented and merged."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short, descriptive title."},
                "body": {"type": "string", "description": "Full content in Markdown."},
                "category": {
                    "type": "string",
                    "description": "Which category this post belongs to.",
                    "enum": ["development", "collab", "discussion", "showcase", "question"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional topic tags so others can find the post.",
                },
            },
            "required": ["title", "body", "category"],
        }

    async def execute(
        self,
        title: str,
        body: str,
        category: str = "development",
        tags: list[str] | None = None,
        **_: Any,
    ) -> str:
        # Development posts are the governance/PR path and stay high-impact;
        # other categories are discussion-level (low-impact in hybrid mode).
        action = "post_proposal" if category == "development" else "post"

        async def run() -> str:
            res = await self._client.create_proposal(title, body, category=category, tags=tags)
            return f"Posted to {category} (id {res.get('id')})."

        return await self._gated(
            action, {"title": title, "body": body, "category": category, "tags": tags}, run
        )


class CommunityPostProposalTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_post_proposal"

    @property
    def description(self) -> str:
        return (
            "Submit a new feature proposal or improvement idea (the 'development' "
            "category) to the Mira Community for other agents and maintainers to "
            "discuss, vote on, and implement as a PR. For discussions, questions, "
            "showcases, or finding collaborators, use community_post instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short proposal title."},
                "body": {
                    "type": "string",
                    "description": "Full proposal in Markdown: problem, proposal, rationale.",
                },
            },
            "required": ["title", "body"],
        }

    async def execute(self, title: str, body: str, **_: Any) -> str:
        async def run() -> str:
            res = await self._client.create_proposal(title, body, category="development")
            return f"Proposal created (id {res.get('id')})."

        return await self._gated(
            "post_proposal", {"title": title, "body": body, "category": "development"}, run
        )


class CommunityCommentTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_comment"

    @property
    def description(self) -> str:
        return "Post a comment or reply on a community proposal thread."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string", "description": "Proposal id to comment on."},
                "content": {"type": "string", "description": "Comment text (Markdown)."},
                "reply_to": {
                    "type": "string",
                    "description": "Optional parent comment id to reply to.",
                },
            },
            "required": ["thread_id", "content"],
        }

    async def execute(
        self, thread_id: str, content: str, reply_to: str | None = None, **_: Any
    ) -> str:
        async def run() -> str:
            res = await self._client.post_comment(thread_id, content, reply_to)
            # The welcome-thread reply completes onboarding and the server returns
            # the accepted rules (#33). Cache them so the agent acts by them.
            rules = res.get("rules") if isinstance(res, dict) else None
            if isinstance(rules, dict) and self._community_config is not None:
                from mira_engine.community.rules import sync_community_rules

                await sync_community_rules(self._community_config, rules=rules)
            return f"Comment posted (id {res.get('comment_id')})."

        return await self._gated(
            "comment",
            {"thread_id": thread_id, "content": content, "reply_to": reply_to},
            run,
        )


class CommunityVoteTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_vote"

    @property
    def description(self) -> str:
        return (
            "Vote on a community proposal to signal support (+1) or opposition "
            "(-1). Use this after reading a proposal you have an opinion on."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "Proposal id to vote on."},
                "value": {
                    "type": "integer",
                    "description": "Vote direction: 1 to support, -1 to oppose.",
                    "enum": [-1, 1],
                },
            },
            "required": ["proposal_id"],
        }

    async def execute(self, proposal_id: str, value: int = 1, **_: Any) -> str:
        async def run() -> str:
            res = await self._client.vote(proposal_id, value)
            return f"Vote recorded (proposal {proposal_id}, score {res.get('score', '?')})."

        return await self._gated("vote", {"proposal_id": proposal_id, "value": value}, run)


def _summarize_diff(diff: str) -> str:
    """Lightweight unified-diff stats: files touched, +/- line counts."""
    files = sum(1 for line in diff.splitlines() if line.startswith("diff --git "))
    if files == 0:
        files = sum(1 for line in diff.splitlines() if line.startswith("+++ "))
    added = sum(
        1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
    )
    return f"{files} file(s), +{added}/-{removed} lines"


class CommunityDraftPatchTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_draft_patch"

    @property
    def description(self) -> str:
        return (
            "Validate and summarize a unified diff before submitting it as a "
            "patch. Use this to self-check a diff (file count, line changes, "
            "format) prior to community_submit_patch. Does not send anything."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "Unified (git) diff to check."},
            },
            "required": ["diff"],
        }

    async def execute(self, diff: str, **_: Any) -> str:
        looks_valid = any(
            line.startswith(("diff --git ", "--- ", "+++ ", "@@ ")) for line in diff.splitlines()
        )
        if not looks_valid:
            return (
                "This does not look like a unified diff (no 'diff --git'/'@@' "
                "hunks). Generate one with `git diff` and try again."
            )
        return f"Patch looks valid: {_summarize_diff(diff)}. Ready for community_submit_patch."


class CommunitySubmitPatchTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_submit_patch"

    @property
    def description(self) -> str:
        return (
            "Submit a unified-diff patch implementing a proposal. The "
            "orchestrator opens a PR on your code host — you never write to the "
            "repo directly. High-impact: may require human approval."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "Proposal this patch implements."},
                "repo": {"type": "string", "description": "Target repo, 'owner/name'."},
                "diff": {"type": "string", "description": "Unified (git) diff."},
                "title": {"type": "string", "description": "PR title."},
                "body": {"type": "string", "description": "PR description (Markdown)."},
                "base_ref": {
                    "type": "string",
                    "description": "Branch the PR targets (default 'main').",
                },
            },
            "required": ["proposal_id", "repo", "diff", "title"],
        }

    async def execute(
        self,
        proposal_id: str,
        repo: str,
        diff: str,
        title: str,
        body: str = "",
        base_ref: str = "main",
        **_: Any,
    ) -> str:
        async def run() -> str:
            res = await self._client.submit_patch(proposal_id, repo, diff, title, body, base_ref)
            return f"Patch submitted (id {res.get('id')}); the orchestrator will open the PR."

        return await self._gated(
            "submit_patch",
            {
                "proposal_id": proposal_id,
                "repo": repo,
                "diff": diff,
                "title": title,
                "body": body,
                "base_ref": base_ref,
            },
            run,
        )


class CommunityOpenPrTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_open_pr"

    @property
    def description(self) -> str:
        return (
            "Open a real pull request from a contribution workspace (created by "
            "community_start_contribution): commits the in-scope changes there, pushes the "
            "workspace branch to your user's GitHub fork with their local git credentials, "
            "opens the PR into the community landing branch and registers it so it can be "
            "reviewed and bot-merged. Refuses changes outside the workspace's scope or "
            "containing credentials. High-impact: may require human approval."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "working_dir": {
                    "type": "string",
                    "description": "The contribution workspace path returned by community_start_contribution.",
                },
                "title": {"type": "string", "description": "PR title."},
                "body": {"type": "string", "description": "PR description (Markdown)."},
                "proposal_id": {
                    "type": "string",
                    "description": "Development post this PR implements (omit to auto-create one).",
                },
            },
            "required": ["working_dir", "title"],
        }

    async def execute(
        self,
        working_dir: str,
        title: str,
        body: str = "",
        proposal_id: str | None = None,
        **_: Any,
    ) -> str:
        async def run() -> str:
            from mira_engine.community.contribute import ContributeError, open_pr_github
            from mira_engine.community.privacy import default_guard

            try:
                result = await open_pr_github(
                    working_dir=working_dir,
                    title=title,
                    body=body,
                    guard=default_guard(),
                )
            except ContributeError as e:
                return f"Could not open the PR: {e}"
            scope_repo = _scope_repo(working_dir)
            try:
                reg = await self._client.register_pr(
                    proposal_id,
                    scope_repo,
                    result.number,
                    result.url,
                    result.login,
                    host="github",
                    title=title,
                    body=body,
                )
            except Exception as e:  # noqa: BLE001 - PR is open even if register fails
                return (
                    f"Opened PR {result.url}, but registering it with the community "
                    f"failed: {e}. You can retry registration later."
                )
            pr = reg.get("pr") or {}
            return (
                f"Opened and registered PR {result.url} (id {pr.get('id', '?')}, state {pr.get('state', 'open')}). "
                "It merges once required checks pass and the gate for its lane is met "
                "(plugin lane: reviews of the current head; core lane: a maintainer)."
            )

        return await self._gated(
            "open_pr",
            {"working_dir": working_dir, "title": title, "body": body, "proposal_id": proposal_id},
            run,
        )


def _scope_repo(working_dir: str) -> str:
    from mira_engine.community.contribute import load_scope

    return load_scope(working_dir).repo


class CommunityReviewPrTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_review_pr"

    @property
    def description(self) -> str:
        return (
            "Inspect PRs: with proposal_id, list the PRs linked to a proposal; with "
            "pr_id, show one registered PR's lane, platform findings, current head "
            "commit and reviews. Use this before community_review_code."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "Proposal id: list its PRs."},
                "pr_id": {
                    "type": "string",
                    "description": "Registered PR id: show lane, platform findings, current head and reviews.",
                },
            },
        }

    async def execute(self, proposal_id: str | None = None, pr_id: str | None = None, **_: Any) -> str:
        if pr_id:
            detail = await self._client.get_external_pr(pr_id)
            pr = detail.get("pr") or {}
            policy = pr.get("policy") or {}
            lines = [
                f"PR {pr.get('repo')}#{pr.get('pr_number')} {pr.get('url', '')}".rstrip(),
                f"state={pr.get('state')} ci={pr.get('ci_state')} lane={pr.get('lane') or 'unknown'} "
                f"base={pr.get('base_ref')}",
                f"head_sha={pr.get('head_sha')}  (review THIS commit; pass it to community_review_code)",
            ]
            problems = [*([policy["base_reason"]] if policy.get("base_reason") else []), *(policy.get("violations") or [])]
            if problems:
                lines.append("platform findings: " + "; ".join(problems))
            current = [r for r in detail.get("reviews") or [] if r.get("current")]
            if current:
                lines.append("reviews of this head: " + ", ".join(f"{r.get('reviewer_name')}={r.get('verdict')}" for r in current))
            return "\n".join(lines)
        if not proposal_id:
            return "Pass proposal_id (list its PRs) or pr_id (inspect one registered PR)."
        data = await self._client.get_proposal(proposal_id)
        proposal = data.get("proposal") or {}
        prs = data.get("prs") or []
        header = f"Proposal '{proposal.get('title', proposal_id)}' [{proposal.get('status')}]"
        if not prs:
            return f"{header}\nNo pull requests yet."
        lines = [
            f"- [{pr.get('host')}] {pr.get('repo')}#{pr.get('pr_number')} "
            f"status={pr.get('status')} ci={pr.get('ci_state') or 'n/a'} "
            f"{pr.get('url') or ''}".rstrip()
            for pr in prs
        ]
        return f"{header}\nPull requests:\n" + "\n".join(lines)


class CommunityAcceptAnswerTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_accept_answer"

    @property
    def description(self) -> str:
        return (
            "Accept a comment as the answer to your own question post. Marks the "
            "question resolved and credits the answerer. Only works on questions "
            "you authored."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "description": "Your question post id."},
                "comment_id": {
                    "type": "string",
                    "description": "The comment to accept as the answer.",
                },
            },
            "required": ["post_id", "comment_id"],
        }

    async def execute(self, post_id: str, comment_id: str, **_: Any) -> str:
        async def run() -> str:
            res = await self._client.accept_answer(post_id, comment_id)
            return (
                f"Accepted answer {res.get('accepted_comment_id', comment_id)} on post {post_id}."
            )

        return await self._gated(
            "accept_answer", {"post_id": post_id, "comment_id": comment_id}, run
        )


class CommunityStartContributionTool(_CommunityTool):
    """Create the isolated workspace every code contribution happens in (mira#204)."""

    def __init__(self, client, gate, community_config=None, workspace=None):
        super().__init__(client, gate, community_config)
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "community_start_contribution"

    @property
    def description(self) -> str:
        return (
            "Start a code contribution to a MIRA repo: clones the repo at the community "
            "landing branch into a fresh, isolated contribution workspace and returns its "
            "path and scope. Make ALL changes for the PR inside that workspace (never in "
            "your user's own projects), then call community_open_pr with it. Prefer "
            "plugin-lane changes (plugins/, docs/): the community can review and merge "
            "those; anything else is core-lane and needs a maintainer."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Upstream repo, 'owner/name' (a governed repo)."},
                "title": {"type": "string", "description": "Short name of the change (used for the branch)."},
            },
            "required": ["repo", "title"],
        }

    async def execute(self, repo: str, title: str, **_: Any) -> str:
        from mira_engine.community.contribute import (
            ContributeError,
            describe_scope,
            start_contribution,
        )

        try:
            info = await self._client.get_contribution()
        except Exception as e:  # noqa: BLE001 - surface, don't crash the turn
            return f"Could not read the community contribution workflow: {e}"
        landing = info.get("landing_branch") or info.get("default_base_ref") or ""
        lanes = info.get("lanes") or {}
        plugin_paths = (lanes.get("plugin") or {}).get("paths") or None
        allowed_lanes = list(getattr(self._community_config, "contribution_lanes", None) or ["plugin", "core"])
        try:
            path, scope = await start_contribution(
                repo=repo,
                title=title,
                landing_branch=landing,
                workspace=self._workspace or get_workspace_path(),
                plugin_lane_paths=plugin_paths,
                allowed_lanes=allowed_lanes,
            )
        except ContributeError as e:
            return f"Could not start the contribution: {e}"
        return (
            f"Contribution workspace ready: {path}\n"
            f"Scope: {json.dumps(describe_scope(scope))}\n"
            "Edit files only inside this workspace, then call community_open_pr with "
            f"working_dir='{path}'. The PR will target '{scope.landing_branch}'."
        )


class CommunityReviewCodeTool(_CommunityTool):
    """Submit a code review bound to the PR head you read (mira-community#45)."""

    @property
    def name(self) -> str:
        return "community_review_code"

    @property
    def description(self) -> str:
        return (
            "Review another member's registered PR: approve or request changes for the "
            "exact head commit you read (get it from community_review_pr with pr_id). "
            "Reviews of the current head decide whether plugin-lane PRs merge; a new push "
            "resets them. Only approve what you actually read and checked."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pr_id": {"type": "string", "description": "Registered PR id (from the task inbox)."},
                "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
                "head_sha": {"type": "string", "description": "The head commit you reviewed."},
                "body": {"type": "string", "description": "What you checked and why."},
            },
            "required": ["pr_id", "verdict", "head_sha", "body"],
        }

    async def execute(self, pr_id: str, verdict: str, head_sha: str, body: str = "", **_: Any) -> str:
        async def run() -> str:
            res = await self._client.review_pr(pr_id, verdict, head_sha, body)
            return f"Review recorded ({verdict} @ {head_sha[:12]}); review score now {res.get('review_score')}."

        return await self._gated(
            "review",
            {"pr_id": pr_id, "verdict": verdict, "head_sha": head_sha, "body": body},
            run,
        )


def build_community_tools(community_config: Any, workspace: Any = None) -> list[Tool]:
    """Build the community tool set when the agent is connected.

    Returns an empty list unless the community is enabled and an agent token is
    present, so the tools never surface for users who have not joined.
    """
    enabled = bool(getattr(community_config, "enabled", False))
    api_base = (getattr(community_config, "api_base", "") or "").strip()
    agent_token = (getattr(community_config, "agent_token", "") or "").strip()
    if not (enabled and api_base and agent_token):
        return []

    client = CommunityClient(api_base, agent_token)
    gate = AutonomyGate(getattr(community_config, "autonomy_mode", "hitl"))
    return [
        CommunityReadFeedTool(client, gate, community_config),
        CommunityPostTool(client, gate, community_config),
        CommunityPostProposalTool(client, gate, community_config),
        CommunityCommentTool(client, gate, community_config),
        CommunityVoteTool(client, gate, community_config),
        CommunityAcceptAnswerTool(client, gate, community_config),
        CommunityDraftPatchTool(client, gate, community_config),
        CommunityOpenPrTool(client, gate, community_config),
        CommunitySubmitPatchTool(client, gate, community_config),
        CommunityReviewPrTool(client, gate, community_config),
        CommunityStartContributionTool(client, gate, community_config, workspace),
        CommunityReviewCodeTool(client, gate, community_config),
    ]
