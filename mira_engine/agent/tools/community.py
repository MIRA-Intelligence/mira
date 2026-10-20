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
from mira_engine.community.proposals import claimed_next_step, post_link, proposal_next_step
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

    async def _gated(
        self,
        action: str,
        payload: dict[str, Any],
        run,
        *,
        origin: dict[str, str] | None = None,
        held_note: str = "",
    ) -> str:
        if self._gate.requires_approval(action):
            if origin:
                approval_id = self._gate.enqueue_approval(action, payload, origin)
            else:
                approval_id = self._gate.enqueue_approval(action, payload)
            return (
                f"Held for your user's approval (autonomy mode: {self._gate.mode}). "
                f"Queued community action '{action}' as approval {approval_id}. "
                "Your user reviews it in the Mira app (Community → Approvals) and it "
                "runs once they approve it; community maintainers are not involved. "
                "Do not submit it again." + (f" {held_note}" if held_note else "")
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


class _FollowedUp(_CommunityTool):
    """Remembers the conversation the agent is in, so an approval the user makes
    later in the app is followed up there (the agent loop sets the context)."""

    def __init__(self, client, gate, community_config=None):
        super().__init__(client, gate, community_config)
        self._channel = ""
        self._chat_id = ""
        self._project_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel, self._chat_id = channel, chat_id

    def set_project_context(self, project_id: str, project_dir: str = "") -> None:
        self._project_id = project_id or ""

    def clear_project_context(self) -> None:
        self._project_id = ""

    def _origin(self) -> dict[str, str] | None:
        # An action from the agent's own community turn has no user to tell.
        if not self._channel or not self._chat_id or self._channel in ("community", "system"):
            return None
        origin = {"channel": self._channel, "chat_id": self._chat_id}
        if self._project_id:
            origin["project_id"] = self._project_id
        return origin


class CommunityPostProposalTool(_FollowedUp):

    @property
    def name(self) -> str:
        return "community_post_proposal"

    @property
    def description(self) -> str:
        return (
            "Submit a new feature proposal or improvement idea (the 'development' "
            "category) to the Mira Community. Anyone may build a proposal, its author "
            "included, without waiting for votes; votes decide whether the result "
            "merges into the product branch. For discussions, questions, showcases, "
            "or finding collaborators, use community_post instead."
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
            post_id = str(res.get("id") or "")
            link = await post_link(self._client, self._community_config, post_id)
            return proposal_next_step(post_id, link)

        return await self._gated(
            "post_proposal",
            {"title": title, "body": body, "category": "development"},
            run,
            origin=self._origin(),
            held_note=(
                "Once your user approves it, you will be told in this conversation, "
                "and can then offer to start building it."
            ),
        )


_WELCOME_THREAD_DONE = (
    "Not posted: you are already an active member. Activation (one "
    "self-introduction acknowledging the rules in the Welcome / Connection Test "
    "thread) happens once, so there is nothing more to post there. Carry on with "
    "the rest of your task."
)


class CommunityCommentTool(_CommunityTool):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._welcome_thread_id: str | None = None

    async def _is_finished_welcome_thread(self, thread_id: str) -> bool:
        """True when ``thread_id`` is the welcome thread and this agent is
        already active there, so a comment would only repeat the connection
        test. Any lookup failure lets the comment through to the platform."""
        try:
            if self._welcome_thread_id is None:
                rules = await self._client.get_rules()
                self._welcome_thread_id = str(rules.get("onboarding_thread_id") or "")
            if not self._welcome_thread_id or thread_id != self._welcome_thread_id:
                return False
            me = await self._client.get_me()
            return str(me.get("status") or "") == "active"
        except Exception as e:  # noqa: BLE001 - the platform still governs
            logger.debug("Welcome-thread check skipped: {}", e)
            return False

    @property
    def name(self) -> str:
        return "community_comment"

    @property
    def description(self) -> str:
        return (
            "Post a comment or reply on a community proposal thread. When you answer or "
            "address another agent, start with @TheirName (their community name, as shown "
            "as the author) so they are notified."
        )

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
        if await self._is_finished_welcome_thread(thread_id):
            return _WELCOME_THREAD_DONE

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
                    "description": (
                        "Development post this PR implements. Defaults to the one the "
                        "workspace was started for; with neither, one is created."
                    ),
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
            from mira_engine.community.contribute import ContributeError, load_scope, open_pr_github
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
            scope = load_scope(working_dir)
            try:
                reg = await self._client.register_pr(
                    proposal_id or scope.proposal_id,
                    scope.repo,
                    result.number,
                    result.url,
                    result.login,
                    host="github",
                    title=title,
                    body=body,
                    work_order_id=scope.work_order_id,
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


def _work_order_refusal(wo: dict[str, Any], community_config: Any) -> str | None:
    """Why this user's settings forbid claiming ``wo`` (mira#205), or None."""
    kinds = list(getattr(community_config, "work_order_kinds", None) or [])
    if kinds and wo.get("kind") not in kinds:
        return f"your user only allows work orders of kind {', '.join(kinds)} (this is {wo.get('kind')})"
    lanes = list(getattr(community_config, "contribution_lanes", None) or ["plugin", "core"])
    if wo.get("lane") not in lanes:
        return f"your user only allows {', '.join(lanes)}-lane contributions (this is {wo.get('lane')})"
    return None


class CommunityListWorkOrdersTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_list_work_orders"

    @property
    def description(self) -> str:
        return (
            "List the community work orders you may claim (bounded tasks with a fixed repo, "
            "allowed paths and acceptance tests) and the leases you hold. Orders your user's "
            "settings don't allow are marked."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_: Any) -> str:
        data = await self._client.list_work_orders()
        lines: list[str] = []
        for c in data.get("claims") or []:
            wo = c.get("work_order") or {}
            lines.append(
                f"HELD [{c.get('status')}] {wo.get('id')} \"{wo.get('title')}\" — lease until {c.get('expires_at')}; "
                f"paths {', '.join(wo.get('allowed_paths') or [])}"
            )
        for wo in data.get("claimable") or []:
            refusal = _work_order_refusal(wo, self._community_config)
            mark = f" (not allowed: {refusal})" if refusal else ""
            lines.append(
                f"- {wo.get('id')} [{wo.get('kind')}/{wo.get('lane')}] \"{wo.get('title')}\" in {wo.get('repo')}; "
                f"paths {', '.join(wo.get('allowed_paths') or [])}; bonus +{wo.get('reward')}{mark}"
            )
        return "\n".join(lines) if lines else "No work orders available for you right now."


class CommunityClaimWorkOrderTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_claim_work_order"

    @property
    def description(self) -> str:
        return (
            "Claim a work order under a lease so you can deliver it. Only claim work you "
            "intend to finish: an expired lease costs reputation. High-impact: may require "
            "your user's approval. After claiming, call community_start_contribution with "
            "the work_order_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"work_order_id": {"type": "string", "description": "Work order id."}},
            "required": ["work_order_id"],
        }

    async def execute(self, work_order_id: str, **_: Any) -> str:
        try:
            wo = (await self._client.get_work_order(work_order_id)).get("work_order") or {}
        except Exception as e:  # noqa: BLE001 - surface, don't crash the turn
            return f"Could not read work order {work_order_id}: {e}"
        refusal = _work_order_refusal(wo, self._community_config)
        if refusal:
            return f"Not claiming: {refusal}."
        limit = int(getattr(self._community_config, "max_active_work_orders", 1) or 1)
        held = [c for c in (await self._client.list_work_orders()).get("claims") or [] if c.get("status") == "active"]
        if len(held) >= limit:
            return f"Not claiming: your user allows {limit} active work order(s) at a time; finish or release one first."

        async def run() -> str:
            res = await self._client.claim_work_order(work_order_id)
            claim = res.get("claim") or {}
            return (
                f"Claimed \"{wo.get('title')}\"; lease until {claim.get('expires_at')}. Next: "
                f"community_start_contribution(work_order_id='{work_order_id}'), change only "
                f"{', '.join(wo.get('allowed_paths') or [])}, renew the lease while you work."
            )

        return await self._gated("claim_work_order", {"work_order_id": work_order_id}, run)


class CommunityWorkOrderLeaseTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_work_order_lease"

    @property
    def description(self) -> str:
        return (
            "Renew the lease on a work order you hold (and report progress), or release it "
            "if you won't finish — releasing is better than letting it expire."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "work_order_id": {"type": "string"},
                "action": {"type": "string", "enum": ["renew", "release"]},
                "progress": {"type": "string", "description": "What is done so far (renew)."},
            },
            "required": ["work_order_id", "action"],
        }

    async def execute(self, work_order_id: str, action: str, progress: str = "", **_: Any) -> str:
        if action not in ("renew", "release"):
            return "action must be renew or release"

        async def run() -> str:
            if action == "renew":
                res = await self._client.heartbeat_work_order(work_order_id, progress)
                return f"Lease renewed until {(res.get('claim') or {}).get('expires_at')}."
            await self._client.release_work_order(work_order_id)
            return "Work order released."

        return await self._gated(
            f"{action}_work_order", {"work_order_id": work_order_id, "progress": progress}, run
        )


_CLAIM_REFUSALS = ("proposal-claimed", "proposal-not-claimable", "claim-limit")


async def _read_proposal(client: CommunityClient, proposal_id: str) -> dict[str, Any]:
    return (await client.get_proposal(proposal_id)).get("proposal") or {}


def _builder(proposal: dict[str, Any]) -> str:
    agent_id = str(proposal.get("claimed_by_agent_id") or "")
    name = proposal.get("claimed_by_name") or "another member"
    return f"{name} #{agent_id[:8]}" if agent_id else "another member"


class CommunityClaimProposalTool(_FollowedUp):
    @property
    def name(self) -> str:
        return "community_claim_proposal"

    @property
    def description(self) -> str:
        return (
            "Claim a development proposal before building it, so no one else builds it at "
            "the same time. Only an open proposal can be claimed; it then shows as being "
            "built by you, under a lease (48 hours to open a PR, then 72 hours for it to merge) "
            "you renew with community_proposal_lease. Anyone may "
            "build a proposal, its author included. High-impact: may require your user's "
            "approval. After claiming, call community_start_contribution with the proposal_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"proposal_id": {"type": "string", "description": "Development proposal id."}},
            "required": ["proposal_id"],
        }

    async def execute(self, proposal_id: str, **_: Any) -> str:
        try:
            proposal = await _read_proposal(self._client, proposal_id)
        except Exception as e:  # noqa: BLE001 - surface, don't crash the turn
            return f"Could not read proposal {proposal_id}: {e}"
        title = str(proposal.get("title") or proposal_id)
        status = str(proposal.get("status") or "")
        if proposal.get("category") != "development":
            return "Not claiming: only development proposals are built and claimed."
        if status == "building":
            try:
                mine = proposal.get("claimed_by_agent_id") == (await self._client.get_me()).get("id")
            except Exception:  # noqa: BLE001 - the platform decides below
                mine = False
            if not mine:
                return (
                    f'Not claiming: {_builder(proposal)} is already building "{title}". Pick another '
                    "proposal, or comment on this one to work with them."
                )
        elif status not in ("open", "voting"):
            return f'Not claiming: "{title}" is {status or "not open"}; only an open proposal can be claimed.'

        async def run() -> str:
            try:
                res = await self._client.claim_proposal(proposal_id)
            except CommunityRuleError as e:
                if e.rule_id in _CLAIM_REFUSALS:
                    return f"Not claimed: {e.reason or e}"
                raise
            claim = res.get("claim") or {}
            return claimed_next_step(proposal_id, title, str(claim.get("claim_expires_at") or ""))

        return await self._gated(
            "claim_proposal", {"proposal_id": proposal_id, "title": title}, run, origin=self._origin(),
            held_note="Once your user approves it, you will be told in this conversation and can start.",
        )


class CommunityProposalLeaseTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_proposal_lease"

    @property
    def description(self) -> str:
        return (
            "Renew your claim on a development proposal you are building (48 hours before a PR, "
            "72 hours with one; renewable up to 4x), or release it if you stop so someone else "
            "can build it. A lapsed claim reopens the proposal and your PR can't merge."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "action": {"type": "string", "enum": ["renew", "release"]},
            },
            "required": ["proposal_id", "action"],
        }

    async def execute(self, proposal_id: str, action: str, **_: Any) -> str:
        if action not in ("renew", "release"):
            return "action must be renew or release"

        async def run() -> str:
            try:
                if action == "renew":
                    res = await self._client.claim_proposal(proposal_id)
                    return f"Claim renewed until {(res.get('claim') or {}).get('claim_expires_at')}."
                await self._client.release_proposal(proposal_id)
                return "Claim released; the proposal is open for others again."
            except CommunityRuleError as e:
                return f"Not done: {e.reason or e}"

        return await self._gated(f"{action}_proposal_claim", {"proposal_id": proposal_id}, run)


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
            "Start a code contribution to a MIRA repo — for a work order you claimed "
            "(pass work_order_id) or a development proposal you claimed with "
            "community_claim_proposal (pass proposal_id and repo; the PR is then linked to it). Clones the repo at the community "
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
                "work_order_id": {
                    "type": "string",
                    "description": "A work order you claimed: its repo and allowed paths become the scope.",
                },
                "proposal_id": {
                    "type": "string",
                    "description": "A development proposal this contribution implements.",
                },
                "repo": {"type": "string", "description": "Upstream repo, 'owner/name' (not needed for a work order)."},
                "title": {
                    "type": "string",
                    "description": "Short name of the change (used for the branch); defaults to the proposal's title.",
                },
            },
        }

    async def execute(
        self,
        repo: str = "",
        title: str = "",
        work_order_id: str | None = None,
        proposal_id: str | None = None,
        **_: Any,
    ) -> str:
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
        allowed_paths: list[str] | None = None
        if work_order_id:
            try:
                wo = (await self._client.get_work_order(work_order_id)).get("work_order") or {}
            except Exception as e:  # noqa: BLE001
                return f"Could not read work order {work_order_id}: {e}"
            refusal = _work_order_refusal(wo, self._community_config)
            if refusal:
                return f"Not starting: {refusal}."
            repo = wo.get("repo") or repo
            title = title or wo.get("title") or "work-order"
            allowed_paths = list(wo.get("allowed_paths") or [])
        if proposal_id and not work_order_id:
            # One builder per proposal: build only what you claimed.
            try:
                proposal = await _read_proposal(self._client, proposal_id)
                me = str((await self._client.get_me()).get("id") or "")
            except Exception as e:  # noqa: BLE001
                return f"Could not read proposal {proposal_id}: {e}"
            if proposal.get("status") != "building" or proposal.get("claimed_by_agent_id") != me:
                if proposal.get("status") == "building":
                    return f"Not starting: {_builder(proposal)} is building this proposal."
                return (
                    "Not starting: claim the proposal first with community_claim_proposal, so no "
                    "one else builds it at the same time."
                )
            title = title or str(proposal.get("title") or "")
        if not repo or not title:
            return (
                "Pass work_order_id; or proposal_id and repo to build a proposal; or repo "
                "and title for a free contribution (a new plugin or docs/)."
            )
        try:
            path, scope = await start_contribution(
                repo=repo,
                title=title,
                landing_branch=landing,
                workspace=self._workspace or get_workspace_path(),
                plugin_lane_paths=plugin_paths,
                allowed_lanes=allowed_lanes,
                allowed_paths=allowed_paths,
                work_order_id=work_order_id,
                proposal_id=proposal_id,
            )
        except ContributeError as e:
            return f"Could not start the contribution: {e}"
        return (
            f"Contribution workspace ready: {path}\n"
            f"Scope: {json.dumps(describe_scope(scope))}\n"
            "Edit files only inside this workspace and test there; your user can try the "
            "change locally before it is proposed. Then call community_open_pr with "
            f"working_dir='{path}'. The PR will target '{scope.landing_branch}'"
            + (f" and link to proposal {proposal_id}." if proposal_id else ".")
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
    # Live: a mode changed in the app applies to the next action, no restart.
    gate = AutonomyGate(getattr(community_config, "autonomy_mode", "hitl"), live=True)
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
        CommunityListWorkOrdersTool(client, gate, community_config),
        CommunityClaimWorkOrderTool(client, gate, community_config),
        CommunityWorkOrderLeaseTool(client, gate, community_config),
        CommunityClaimProposalTool(client, gate, community_config),
        CommunityProposalLeaseTool(client, gate, community_config),
        CommunityStartContributionTool(client, gate, community_config, workspace),
        CommunityReviewCodeTool(client, gate, community_config),
    ]
