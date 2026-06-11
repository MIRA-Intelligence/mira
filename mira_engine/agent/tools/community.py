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
                    "description": "Max proposals to fetch (1-100).",
                    "minimum": 1,
                    "maximum": 100,
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter (open, voting, building, merged).",
                },
            },
        }

    async def execute(
        self, limit: int = 20, status: str | None = None, **_: Any
    ) -> str:
        data = await self._client.read_feed(limit=limit, status=status)
        proposals = data.get("proposals", [])
        if not proposals:
            return "Community feed is empty."
        lines = [
            f"- [{p.get('status')}] {p.get('title')} "
            f"(score {p.get('score', 0)}, {p.get('comment_count', 0)} comments) "
            f"by {p.get('author_handle')} — id {p.get('id')}"
            for p in proposals
        ]
        return "Recent community proposals:\n" + "\n".join(lines)


class CommunityPostProposalTool(_CommunityTool):
    @property
    def name(self) -> str:
        return "community_post_proposal"

    @property
    def description(self) -> str:
        return (
            "Submit a new feature proposal or improvement idea to the Mira "
            "Community for other agents and maintainers to discuss and vote on."
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
            res = await self._client.create_proposal(title, body)
            return f"Proposal created (id {res.get('id')})."

        return await self._gated("post_proposal", {"title": title, "body": body}, run)


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
