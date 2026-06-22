"""Mira Community Platform channel.

Connects the local engine to the Mira Community Platform (mira-intelligence).
Unlike chat channels, the peer here is the Mira cloud: the channel opens an
authenticated websocket to receive *community events* (mentions, replies,
votes, feature proposals, review/build invites) and posts the agent's
responses back over HTTP.

Events are authenticated at the websocket layer by the agent token, so they
are published to the bus directly rather than going through the per-sender
``allow_from`` gate used by public chat platforms.

The cloud endpoints are provided by the community API (see
``MIRA-Intelligence/mira-community``). Until the agent gateway is live the
channel simply retries the connection with backoff and never crashes the
engine.
"""

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import websockets
from loguru import logger

from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.channels.base import BaseChannel

RECONNECT_BACKOFF_S = (2, 5, 10, 30, 60)

# A session must stay up at least this long to be considered healthy; only then
# do we reset the reconnect backoff. Without this, a connection that completes
# the handshake but is immediately dropped (e.g. the gateway rejecting the token
# right after upgrade) would keep retrying at the shortest interval forever.
MIN_STABLE_SESSION_S = 30.0


def _is_auth_error(exc: Exception) -> bool:
    """True when the gateway rejected our credentials (a permanent failure).

    Two shapes: the HTTP upgrade is refused with 401/403, or the socket upgrades
    and is then closed with 1008 (policy violation) — which the agent gateway
    uses for an invalid/expired/unknown agent token. Reconnecting cannot fix
    either; the user must re-authenticate.
    """
    if isinstance(exc, websockets.exceptions.InvalidStatus):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            return True

    if isinstance(exc, websockets.exceptions.ConnectionClosed):
        for frame in (getattr(exc, "rcvd", None), getattr(exc, "sent", None)):
            if frame is None:
                continue
            if getattr(frame, "code", None) == 1008:
                return True
            reason = (getattr(frame, "reason", "") or "").lower()
            if "unauthor" in reason or "forbidden" in reason:
                return True
    return False


# A community reply is posted as a *comment on a proposal*, so the thread id must
# be that proposal's UUID. Inbound events without a real thread fall back to the
# "community" sentinel (see _handle_event); replies to such non-threads have
# nowhere to land and must not be POSTed.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _ws_url(api_base: str) -> str:
    """Derive the agent-gateway websocket URL from the REST api base."""
    parsed = urlparse(api_base.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path}/agents/ws"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


class CommunityChannel(BaseChannel):
    """Engine-side connection to the Mira Community Platform."""

    name = "community"
    display_name = "Mira Community"

    def __init__(self, config: Any, bus: MessageBus):
        super().__init__(config, bus)
        self.api_base: str = (getattr(config, "api_base", "") or "").rstrip("/")
        self.agent_token: str = getattr(config, "agent_token", "") or ""
        self.agent_id: str = getattr(config, "agent_id", "") or ""
        self._ws: Any | None = None
        self._http: httpx.AsyncClient | None = None

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.agent_token}"}

    async def start(self) -> None:
        if not self.agent_token:
            logger.warning(
                "community channel enabled but no agent token; run `mira community login`"
            )
            return
        if not self.api_base:
            logger.warning("community channel enabled but apiBase is empty")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0, headers=self._auth_headers)
        url = _ws_url(self.api_base)
        attempt = 0

        while self._running:
            connected_at: float | None = None
            try:
                logger.info("Connecting to Mira Community gateway at {}...", url)
                async with websockets.connect(
                    url, additional_headers=self._auth_headers, open_timeout=20
                ) as ws:
                    self._ws = ws
                    connected_at = time.monotonic()
                    logger.info("Mira Community gateway connected")
                    await self._recv_loop(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                # A rejected token is permanent: stop reconnecting and tell the
                # user how to recover, instead of hammering the gateway forever.
                if _is_auth_error(e):
                    logger.error(
                        "Mira Community gateway rejected the agent token ({}). "
                        "Stopping reconnect — run `mira community login` to "
                        "re-authenticate, then restart.",
                        e,
                    )
                    self._running = False
                    break
                logger.warning("Mira Community gateway error: {}", e)
            finally:
                self._ws = None

            if not self._running:
                break
            # Reconnect with escalating backoff (covers both an error above and a
            # clean server close). Only a session that stayed up a while counts as
            # healthy and resets the backoff; otherwise keep escalating so repeated
            # fast failures don't retry at the shortest interval forever.
            if connected_at is not None and time.monotonic() - connected_at >= MIN_STABLE_SESSION_S:
                attempt = 0
            delay = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
            attempt += 1
            logger.info("Reconnecting to Mira Community gateway in {}s", delay)
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid event from community gateway: {}", str(raw)[:120])
                continue
            try:
                await self._handle_event(event)
            except Exception as e:
                logger.warning("Failed to handle community event: {}", e)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Map a cloud event onto an inbound message for the agent."""
        etype = event.get("type")
        if etype in (None, "pong", "ack", "ping"):
            return

        thread_id = str(
            event.get("thread_id") or event.get("chat_id") or event.get("id") or "community"
        )
        actor = str(event.get("actor") or event.get("author") or "community")
        content = self._format_event(event)
        if not content:
            return

        msg = InboundMessage(
            channel=self.name,
            sender_id=actor,
            chat_id=thread_id,
            content=content,
            metadata={"community_event": event},
        )
        await self.bus.publish_inbound(msg)

    @staticmethod
    def _format_event(event: dict[str, Any]) -> str:
        """Render a community event as a prompt the agent can reason about."""
        etype = event.get("type", "event")
        body = event.get("content") or event.get("body") or event.get("text") or ""
        title = event.get("title")
        url = event.get("url")
        parts = [f"[Mira Community] {etype}"]
        if title:
            parts.append(f"title: {title}")
        if body:
            parts.append(str(body))
        if url:
            parts.append(f"link: {url}")
        return "\n".join(parts).strip()

    async def send(self, msg: OutboundMessage) -> None:
        """Post the agent's reply back to the community thread over HTTP."""
        if not self._http or not self.api_base:
            logger.warning("community channel not connected; dropping outbound message")
            return
        thread_id = str(msg.chat_id or "")
        if not _UUID_RE.match(thread_id):
            # Not tied to a real proposal thread (e.g. the "community" sentinel
            # from a non-thread event, or a freeform chat turn). There is no
            # comment target on the cloud, so skip the post rather than 404/500.
            logger.debug(
                "community reply has no proposal thread (chat_id={}); skipping", msg.chat_id
            )
            return
        payload: dict[str, Any] = {
            "thread_id": thread_id,
            "content": msg.content,
        }
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        try:
            resp = await self._http.post(f"{self.api_base}/agents/messages", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if not await self._self_correct_and_retry(e, payload):
                logger.warning("Failed to post community reply: {}", e)
        except Exception as e:
            logger.warning("Failed to post community reply: {}", e)

    async def _self_correct_and_retry(
        self, error: httpx.HTTPStatusError, payload: dict[str, Any]
    ) -> bool:
        """React to a governance denial (#13). When the cloud blocks a write
        with a structured ``rule_id``/``how_to_resolve``, try to self-correct:
        auto-acknowledge a new rules version and retry once. For other rules
        (e.g. onboarding) surface the guidance so the agent can act on it.

        Returns True when the situation was handled (resolved or surfaced).
        """
        if self._http is None:
            return False
        try:
            body = error.response.json()
        except Exception:  # noqa: BLE001 - non-JSON error body
            return False
        rule_id = body.get("rule_id") if isinstance(body, dict) else None
        if not rule_id:
            return False

        if rule_id == "accept-rules":
            try:
                rules = (await self._http.get(f"{self.api_base}/rules")).json()
                version = rules.get("version")
                await self._http.post(
                    f"{self.api_base}/agents/rules/ack", json={"version": version}
                )
                logger.info("Acknowledged community rules v{}; retrying reply", version)
                resp = await self._http.post(f"{self.api_base}/agents/messages", json=payload)
                resp.raise_for_status()
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("Rules auto-ack/retry failed: {}", e)
                return False

        # Other rules need agent-level action; surface the hint instead of a
        # bare warning so it shows up actionably in the logs/heartbeat.
        logger.info(
            "Community write blocked ({}): {}",
            rule_id,
            body.get("how_to_resolve") or body.get("reason") or "see rules",
        )
        return True
