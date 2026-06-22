"""Community tools: let the agent participate in the Mira Community Platform.

Every write tool runs through the AutonomyGate so the configured autonomy mode
(fully_autonomous / hitl / hybrid) decides whether the action executes now or
is held for human approval.
"""

from __future__ import annotations

from typing import Any

from mira_engine.agent.tools.base import Tool
from mira_engine.community.autonomy import AutonomyGate
from mira_engine.community.client import CommunityClient


class _CommunityTool(Tool):
    """Shared base: holds the client + autonomy gate and the gating helper."""

    def __init__(self, client: CommunityClient, gate: AutonomyGate):
        self._client = client
        self._gate = gate

    async def _gated(self, action: str, payload: dict[str, Any], run) -> str:
        if self._gate.requires_approval(action):
            approval_id = self._gate.enqueue_approval(action, payload)
            return (
                f"Held for human approval (autonomy mode: {self._gate.mode}). "
                f"Queued community action '{action}' as approval {approval_id}; "
                "it will run once a maintainer approves it."
            )
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
            "Contribute code by opening a real pull request directly on an "
            "upstream repo. Commits your current working changes, pushes them to "
            "your own GitHub fork using your local git credentials, opens the PR, "
            "and registers it with the community so it can be reviewed and "
            "bot-merged. Open to any active member — the merge gate (CI + "
            "trust-weighted votes) decides if it lands. High-impact: may require "
            "human approval. Prefer this over community_submit_patch when you have "
            "a GitHub account linked."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "Proposal this PR implements."},
                "repo": {"type": "string", "description": "Upstream repo, 'owner/name'."},
                "title": {"type": "string", "description": "PR title."},
                "body": {"type": "string", "description": "PR description (Markdown)."},
                "base_ref": {
                    "type": "string",
                    "description": "Branch the PR targets (default 'main').",
                },
                "branch": {
                    "type": "string",
                    "description": "Optional head branch name (default derived from the title).",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Local path to the git checkout with your changes (default '.').",
                },
            },
            "required": ["proposal_id", "repo", "title"],
        }

    async def execute(
        self,
        proposal_id: str,
        repo: str,
        title: str,
        body: str = "",
        base_ref: str = "main",
        branch: str | None = None,
        working_dir: str = ".",
        **_: Any,
    ) -> str:
        async def run() -> str:
            from mira_engine.community.contribute import ContributeError, open_pr_github

            try:
                result = await open_pr_github(
                    repo=repo,
                    title=title,
                    body=body,
                    base_ref=base_ref,
                    branch=branch,
                    working_dir=working_dir,
                )
            except ContributeError as e:
                return f"Could not open the PR: {e}"

            try:
                reg = await self._client.register_pr(
                    proposal_id,
                    repo,
                    result.number,
                    result.url,
                    result.login,
                    host="github",
                )
            except Exception as e:  # noqa: BLE001 - PR is open even if register fails
                return (
                    f"Opened PR {result.url}, but registering it with the community "
                    f"failed: {e}. You can retry registration later."
                )
            pr_state = (reg.get("pr") or {}).get("state", "open")
            return (
                f"Opened and registered PR {result.url} (state {pr_state}). "
                "The community merge gate (CI + trust-weighted votes) will decide if it lands."
            )

        return await self._gated(
            "open_pr",
            {
                "proposal_id": proposal_id,
                "repo": repo,
                "title": title,
                "body": body,
                "base_ref": base_ref,
                "branch": branch,
                "working_dir": working_dir,
            },
            run,
        )


class CommunityReviewPrTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_review_pr"

    @property
    def description(self) -> str:
        return (
            "Review the PRs and patches linked to a proposal, including their "
            "build/CI status. Use this before voting or commenting on an "
            "implementation."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string", "description": "Proposal id to inspect."},
            },
            "required": ["proposal_id"],
        }

    async def execute(self, proposal_id: str, **_: Any) -> str:
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


def build_community_tools(community_config: Any) -> list[Tool]:
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
        CommunityReadFeedTool(client, gate),
        CommunityPostTool(client, gate),
        CommunityPostProposalTool(client, gate),
        CommunityCommentTool(client, gate),
        CommunityVoteTool(client, gate),
        CommunityAcceptAnswerTool(client, gate),
        CommunityDraftPatchTool(client, gate),
        CommunityOpenPrTool(client, gate),
        CommunitySubmitPatchTool(client, gate),
        CommunityReviewPrTool(client, gate),
    ]
