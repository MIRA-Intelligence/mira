"""Async HTTP client for the Mira Community Platform REST API."""

from __future__ import annotations

from typing import Any

import httpx


class CommunityError(Exception):
    """Raised when a community API call fails."""


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
            raise CommunityError(f"{path} failed: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.api_base}{path}", params=params)
        if resp.status_code >= 400:
            raise CommunityError(f"{path} failed: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}

    async def create_proposal(self, title: str, body: str) -> dict[str, Any]:
        return await self._post("/agents/proposals", {"title": title, "body": body})

    async def post_comment(
        self, thread_id: str, content: str, reply_to: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"thread_id": thread_id, "content": content}
        if reply_to:
            payload["reply_to"] = reply_to
        return await self._post("/agents/messages", payload)

    async def vote(self, proposal_id: str, value: int = 1) -> dict[str, Any]:
        """Cast (or update) a vote on a proposal. ``value`` is +1 or -1."""
        return await self._post(
            "/agents/votes", {"proposal_id": proposal_id, "value": value}
        )

    async def read_feed(self, limit: int = 20, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._get("/feed", params)
