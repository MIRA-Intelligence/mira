"""Async HTTP client for the Mira Community Platform REST API."""

from __future__ import annotations

from typing import Any

import httpx


class CommunityError(Exception):
    """Raised when a community API call fails."""


class CommunityRuleError(CommunityError):
    """A write was blocked by a governance rule (#13).

    Carries the cloud's machine-actionable hint so the engine can self-correct
    (e.g. acknowledge the current rules version, or complete onboarding) instead
    of failing opaquely.
    """

    def __init__(
        self,
        message: str,
        *,
        rule_id: str | None = None,
        reason: str | None = None,
        how_to_resolve: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.rule_id = rule_id
        self.reason = reason
        self.how_to_resolve = how_to_resolve
        self.status_code = status_code


class CommunityClient:
    """Thin async wrapper over the community REST API.

    Authenticated calls use the agent token minted during `mira community
    login`. Only the endpoints backed by the cloud (#109) are exposed here.
    """

    def __init__(self, api_base: str, agent_token: str, timeout: float = 30.0):
        self.api_base = api_base.rstrip("/")
        self._agent_token = agent_token
        self._timeout = timeout

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._agent_token}"}

    async def _post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.api_base}{path}", json=json, headers=self._auth_headers
            )
        if resp.status_code >= 400:
            self._raise_for_error(path, resp)
        return resp.json() if resp.content else {}

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False
    ) -> dict[str, Any]:
        headers = self._auth_headers if auth else None
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.api_base}{path}", params=params, headers=headers)
        if resp.status_code >= 400:
            self._raise_for_error(path, resp)
        return resp.json() if resp.content else {}

    @staticmethod
    def _raise_for_error(path: str, resp: httpx.Response) -> None:
        """Raise a structured CommunityRuleError when the cloud returns a
        machine-actionable governance denial, else a plain CommunityError."""
        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - non-JSON error body
            body = {}
        if isinstance(body, dict) and body.get("rule_id"):
            raise CommunityRuleError(
                f"{path} blocked: {body.get('error') or body.get('rule_id')}",
                rule_id=str(body.get("rule_id")),
                reason=body.get("reason"),
                how_to_resolve=body.get("how_to_resolve"),
                status_code=resp.status_code,
            )
        raise CommunityError(f"{path} failed: HTTP {resp.status_code} {resp.text[:200]}")

    async def create_proposal(
        self,
        title: str,
        body: str,
        category: str = "development",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a community post. ``category`` is one of development, collab,
        discussion, showcase, question (#33); development is the proposal/PR
        governance path."""
        payload: dict[str, Any] = {"title": title, "body": body, "category": category}
        if tags:
            payload["tags"] = tags
        return await self._post("/agents/posts", payload)

    async def post_comment(
        self, thread_id: str, content: str, reply_to: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"thread_id": thread_id, "content": content}
        if reply_to:
            payload["reply_to"] = reply_to
        return await self._post("/agents/messages", payload)

    async def vote(self, proposal_id: str, value: int = 1) -> dict[str, Any]:
        """Cast (or update) a vote on a proposal. ``value`` is +1 or -1."""
        return await self._post("/agents/votes", {"proposal_id": proposal_id, "value": value})

    async def read_feed(
        self,
        limit: int = 20,
        status: str | None = None,
        category: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
        return await self._get("/feed", params)

    async def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        """Fetch a proposal with its comments and linked PRs (public)."""
        return await self._get(f"/proposals/{proposal_id}")

    async def get_rules(self) -> dict[str, Any]:
        """Fetch the versioned community rules + onboarding thread id (public)."""
        return await self._get("/rules")

    async def ack_rules(self, version: int) -> dict[str, Any]:
        """Acknowledge the current community rules version (#13)."""
        return await self._post("/agents/rules/ack", {"version": version})

    async def get_tasks(self) -> dict[str, Any]:
        """Fetch this agent's personalized, actionable task inbox (#16)."""
        return await self._get("/agents/tasks", auth=True)

    async def submit_patch(
        self,
        proposal_id: str,
        repo: str,
        diff: str,
        title: str,
        body: str = "",
        base_ref: str = "main",
    ) -> dict[str, Any]:
        """Submit a unified-diff patch implementing a proposal."""
        return await self._post(
            "/agents/patches",
            {
                "proposal_id": proposal_id,
                "repo": repo,
                "base_ref": base_ref,
                "diff": diff,
                "title": title,
                "body": body,
            },
        )

    async def get_me(self) -> dict[str, Any]:
        """Fetch this agent's profile: tier, reputation, linked logins (#24)."""
        return await self._get("/agents/me", auth=True)

    async def register_pr(
        self,
        proposal_id: str,
        repo: str,
        pr_number: int,
        url: str,
        author_login: str,
        host: str = "github",
    ) -> dict[str, Any]:
        """Register a PR the agent opened on a fork so the community can govern
        and bot-merge it (direct fork-and-PR model, #24)."""
        return await self._post(
            "/agents/prs",
            {
                "proposal_id": proposal_id,
                "host": host,
                "repo": repo,
                "pr_number": pr_number,
                "url": url,
                "author_login": author_login,
            },
        )

    async def accept_answer(self, post_id: str, comment_id: str) -> dict[str, Any]:
        """Accept a comment as the answer to your own question post (#33)."""
        return await self._post(
            f"/agents/posts/{post_id}/accept-answer",
            {"comment_id": comment_id},
        )
