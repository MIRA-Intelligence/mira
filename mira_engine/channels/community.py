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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
import websockets
from loguru import logger

from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.channels.base import BaseChannel

RECONNECT_BACKOFF_S = (2, 5, 10, 30, 60)
# How often to poll config.json for credentials while idle (not logged in), and
# while connected (to detect logout / a re-login with a new token).
IDLE_POLL_S = 5.0
CRED_WATCH_S = 8.0

# A community reply is posted as a *comment on a proposal*, so the thread id must
# be that proposal's UUID. Inbound events without a real thread fall back to the
# "community" sentinel (see _handle_event); replies to such non-threads have
# nowhere to land and must not be POSTed.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _event_thread_id(event: dict[str, Any]) -> str:
    """Resolve the proposal thread an event's reply should land on.

    Targeted task events (onboarding, mention, needs_vote, ...) arrive as
    ``agent_events`` flushed as ``{type, id, target_id, title, action}``. The
    actionable thread is in ``action.body.thread_id`` (comment tasks) or
    ``action.body.proposal_id`` (vote/review tasks), NOT the event row ``id`` —
    using ``id`` (a bigint) made the agent's reply target a non-UUID thread that
    the cloud has no comment endpoint for, so onboarding replies were dropped.
    Falls back to an explicit thread on the event, then a UUID ``target_id``,
    then the ``community`` sentinel for non-thread events.
    """
    action = event.get("action")
    if isinstance(action, dict):
        body = action.get("body")
        if isinstance(body, dict):
            for key in ("thread_id", "proposal_id"):
                value = body.get(key)
                if value and _UUID_RE.match(str(value)):
                    return str(value)
    for key in ("thread_id", "chat_id"):
        value = event.get(key)
        if value and _UUID_RE.match(str(value)):
            return str(value)
    target = event.get("target_id")
    if target and _UUID_RE.match(str(target)):
        return str(target)
    return "community"


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

    def _read_credentials(self) -> tuple[str, str, str]:
        """Read live credentials from config.json so login/logout take effect
        without a gateway restart. Falls back to the construction snapshot when
        the file can't be read."""
        try:
            from mira_engine.config.loader import get_config_path

            data = json.loads(Path(get_config_path()).read_text())
            c = data.get("community", {}) if isinstance(data, dict) else {}
            if not isinstance(c, dict):
                c = {}
            api_base = (c.get("apiBase") or c.get("api_base") or "").rstrip("/")
            token = c.get("agentToken") or c.get("agent_token") or ""
            agent_id = c.get("agentId") or c.get("agent_id") or ""
            return api_base, token, agent_id
        except Exception:  # noqa: BLE001 - fall back to the snapshot
            return self.api_base, self.agent_token, self.agent_id

    async def start(self) -> None:
        """Run until stopped, managing the connection lifecycle on its own.

        The channel is always created (community is a first-class feature), so it
        idles until credentials exist, auto-connects when the user logs in (CLI
        or UI), reconnects on a re-login with a new token, and disconnects on
        logout — all without a gateway restart.
        """
        self._running = True
        idle_logged = False

        while self._running:
            api_base, token, agent_id = self._read_credentials()
            if not (api_base and token):
                if not idle_logged:
                    logger.info("Community channel idle — waiting for `mira community login`")
                    idle_logged = True
                await asyncio.sleep(IDLE_POLL_S)
                continue
            idle_logged = False

            self.api_base, self.agent_token, self.agent_id = api_base, token, agent_id
            self._http = httpx.AsyncClient(timeout=30.0, headers=self._auth_headers)
            attempt = 0
            try:
                while self._running:
                    # Pick up a re-login (new token) or logout before reconnecting.
                    api_base, token, agent_id = self._read_credentials()
                    if not (api_base and token):
                        break  # logged out → back to idle
                    self.api_base, self.agent_token, self.agent_id = api_base, token, agent_id
                    self._http.headers["Authorization"] = f"Bearer {self.agent_token}"
                    url = _ws_url(self.api_base)
                    try:
                        logger.info("Connecting to Mira Community gateway at {}...", url)
                        async with websockets.connect(
                            url, additional_headers=self._auth_headers, open_timeout=20
                        ) as ws:
                            self._ws = ws
                            attempt = 0
                            logger.info("Mira Community gateway connected")
                            await self._recv_until_creds_change(ws)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        if not self._running:
                            break
                        delay = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
                        attempt += 1
                        logger.warning(
                            "Mira Community gateway error: {} (retry in {}s)", e, delay
                        )
                        await asyncio.sleep(delay)
                    finally:
                        self._ws = None
            finally:
                if self._http:
                    await self._http.aclose()
                    self._http = None

    async def _recv_until_creds_change(self, ws: Any) -> None:
        """Receive events until the socket closes or credentials change on disk
        (logout / re-login), so the outer loop can re-evaluate the connection."""
        watcher = asyncio.create_task(self._watch_credentials(ws))
        try:
            await self._recv_loop(ws)
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _watch_credentials(self, ws: Any) -> None:
        while self._running:
            await asyncio.sleep(CRED_WATCH_S)
            _, token, _ = self._read_credentials()
            if token != self.agent_token:
                logger.info("Community credentials changed; reconnecting")
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
                return

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

        # Lifecycle events deliver rules (#33): cache them and do NOT spawn an
        # agent turn — they are housekeeping, not something to reason about.
        if etype in ("rules_updated", "onboarded"):
            await self._sync_delivered_rules(event)
            return

        thread_id = _event_thread_id(event)
        actor = str(event.get("actor") or event.get("author") or "community")
        if etype == "onboarding":
            # The server frames onboarding as a terse "connection test", which
            # nudges the agent into a canned acknowledgement. Drive an authentic
            # self-introduction instead — the same prompt the manual `mira
            # community onboard` nudge uses, so both paths behave identically.
            from mira_engine.community.onboarding import ONBOARDING_PROMPT

            content = ONBOARDING_PROMPT
        else:
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

    async def _sync_delivered_rules(self, event: dict[str, Any]) -> None:
        """Cache rules delivered by a lifecycle event (#33).

        ``rules_updated``/``onboarded`` carry ``rules: {version, items}``; cache
        them client-side (and persist) so the agent acts by them. Falls back to a
        fetch when the payload is absent (older cloud). Never raises.
        """
        from mira_engine.community.rules import sync_community_rules

        rules = event.get("rules")
        payload = rules if isinstance(rules, dict) else None
        try:
            await sync_community_rules(self.config, rules=payload)
        except Exception as e:  # noqa: BLE001 - housekeeping must never crash recv
            logger.debug("Failed to sync delivered community rules: {}", e)

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
            await self._cache_rules_from_reply(resp)
        except httpx.HTTPStatusError as e:
            if not await self._self_correct_and_retry(e, payload):
                logger.warning("Failed to post community reply: {}", e)
        except Exception as e:
            logger.warning("Failed to post community reply: {}", e)

    async def _cache_rules_from_reply(self, resp: httpx.Response) -> None:
        """When a reply completes onboarding the server returns the accepted
        rules (#33). Cache them so the agent acts by them right away, instead of
        waiting for the next heartbeat sync."""
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - empty/non-JSON body
            return
        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, dict):
            return
        from mira_engine.community.rules import sync_community_rules

        try:
            await sync_community_rules(self.config, rules=rules)
        except Exception as e:  # noqa: BLE001 - caching is a nicety
            logger.debug("Caching rules from onboarding reply failed: {}", e)

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
