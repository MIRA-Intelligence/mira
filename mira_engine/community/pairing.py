"""Device-flow pairing with the Mira Community Platform.

``mira community login`` and the desktop app's ``/api/community/pair/*``
handlers used to talk to the platform on their own, on two HTTP stacks
(httpx and aiohttp) whose trust stores differed: the CLI could join while
the app failed with CERTIFICATE_VERIFY_FAILED (mira#251). Both now go
through the three calls here, on httpx like the rest of the engine's
outbound HTTP, and report failures with one error type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from mira_engine.community.client import CommunityClient, CommunityError

START_TIMEOUT = 30.0
POLL_TIMEOUT = 30.0
ME_TIMEOUT = 8.0


class PairingError(CommunityError):
    """A pairing request failed.

    ``status`` is the HTTP status the platform answered with, None when the
    request never got an answer (``network`` is True then); ``detail`` is
    the reason the platform gave, when it gave one.
    """

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None):
        super().__init__(message, status_code=status, detail=detail)
        self.status = status

    @property
    def network(self) -> bool:
        return self.status is None


@dataclass(frozen=True)
class PairingStart:
    """The platform's answer to pair/start: where the user authorizes the
    agent, the code to type there, and what to poll with. ``raw`` is the
    body as the platform sent it, for callers that pass it on."""

    verification_url: str
    poll_token: str
    user_code: str | None = None
    interval: int = 5
    expires_in: int = 300
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PairingPoll:
    """One pair/poll answer: ``pending`` (keep polling), ``expired`` (start
    over) or ``authorized`` (with the agent's token and id)."""

    status: str
    agent_token: str = ""
    agent_id: str = ""


def _base(api_base: str) -> str:
    return (api_base or "").rstrip("/")


def _json_dict(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _detail(resp: httpx.Response) -> str | None:
    error = _json_dict(resp).get("error")
    return error if isinstance(error, str) and error else None


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _post(url: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, json=payload)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise PairingError(f"network error: {exc}") from exc


async def start_pairing(
    api_base: str, payload: dict[str, Any], *, timeout: float = START_TIMEOUT
) -> PairingStart:
    """``POST /agents/pair/start`` with the body from
    :func:`mira_engine.community.public_profile.pairing_payload`."""
    resp = await _post(f"{_base(api_base)}/agents/pair/start", payload, timeout)
    if resp.status_code == 404:
        raise PairingError("pairing endpoint unavailable on the community service", status=404)
    if resp.status_code >= 400:
        detail = _detail(resp)
        raise PairingError(
            f"pairing failed: {detail or f'HTTP {resp.status_code}'}",
            status=resp.status_code,
            detail=detail,
        )
    data = _json_dict(resp)
    verification_url = data.get("verification_url") or data.get("verification_uri")
    poll_token = data.get("poll_token") or data.get("device_code")
    if not (verification_url and poll_token):
        raise PairingError(
            "unexpected pairing response from the community service", status=resp.status_code
        )
    user_code = data.get("user_code") or data.get("pairing_code")
    return PairingStart(
        verification_url=str(verification_url),
        poll_token=str(poll_token),
        user_code=str(user_code) if user_code else None,
        interval=_int(data.get("interval"), 5),
        expires_in=_int(data.get("expires_in"), 300),
        raw=data,
    )


async def poll_pairing(api_base: str, poll_token: str, *, timeout: float = POLL_TIMEOUT) -> PairingPoll:
    """``POST /agents/pair/poll`` once."""
    resp = await _post(f"{_base(api_base)}/agents/pair/poll", {"poll_token": poll_token}, timeout)
    if resp.status_code in (202, 425):
        return PairingPoll("pending")
    if resp.status_code == 410:
        return PairingPoll("expired")
    if resp.status_code >= 400:
        raise PairingError(
            f"poll failed: HTTP {resp.status_code}", status=resp.status_code, detail=_detail(resp)
        )
    data = _json_dict(resp)
    agent_token = data.get("agent_token")
    if not agent_token:
        return PairingPoll("pending")
    return PairingPoll("authorized", agent_token=str(agent_token), agent_id=str(data.get("agent_id") or ""))


async def fetch_me(api_base: str, agent_token: str, *, timeout: float = ME_TIMEOUT) -> dict[str, Any] | None:
    """Best-effort ``GET /agents/me``: the agent as the platform has it
    (name, lifecycle status, website). None when not logged in, when the
    service is unreachable or when it refuses the token."""
    if not (_base(api_base) and agent_token):
        return None
    try:
        data = await CommunityClient(api_base, agent_token, timeout=timeout).get_me()
    except (CommunityError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    agent = data.get("agent", data) if isinstance(data, dict) else None
    return agent if isinstance(agent, dict) else None
