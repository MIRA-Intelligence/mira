"""Token usage of team providers, and per-user token quotas.

Every request to a team provider goes through Mira Server's LLM proxy
(``/llm/<provider>``), which knows whose engine sent it. The proxy reads the
token counts from the provider's response while streaming it back
(:class:`UsageMeter`) and the store adds them up per user, day, provider and
model. Counts only: never prompts or responses.

Quotas are in tokens: a team default per user, daily and monthly, with
per-user overrides. They are checked before a request is forwarded, so a
request in flight can exceed a limit by at most its own size. Days and months
follow the server's local time. Users' own keys are not metered or limited.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

# A JSON (not streamed) response is parsed whole; larger ones are not metered.
_MAX_JSON_RESPONSE = 16 * 1024 * 1024
_MAX_SSE_LINE = 4 * 1024 * 1024
DEFAULT_WARN_PERCENT = 80
PERIODS = ("daily", "monthly")
_GEMINI_MODEL = re.compile(r"/models/([^/:?]+)")


@dataclass(frozen=True)
class TokenCounts:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage_fields(usage: dict[str, Any]) -> dict[str, int]:
    """The fields one usage object reports (OpenAI, Responses API, Anthropic or Gemini)."""
    found: dict[str, int] = {}
    if "promptTokenCount" in usage or "totalTokenCount" in usage:  # Gemini usageMetadata
        prompt = _count(usage.get("promptTokenCount"))
        total = _count(usage.get("totalTokenCount"))
        cached = _count(usage.get("cachedContentTokenCount"))
        if prompt is not None:
            found["input_tokens"] = prompt
        if total is not None:
            found["total_tokens"] = total
            if prompt is not None:
                found["output_tokens"] = max(total - prompt, 0)  # includes thinking tokens
        elif (candidates := _count(usage.get("candidatesTokenCount"))) is not None:
            found["output_tokens"] = candidates
        if cached is not None:
            found["cached_tokens"] = cached
        return found
    prompt = _count(usage.get("prompt_tokens"))
    if prompt is None:
        prompt = _count(usage.get("input_tokens"))
    output = _count(usage.get("completion_tokens"))
    if output is None:
        output = _count(usage.get("output_tokens"))
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    # Anthropic: input_tokens excludes cache reads and writes; count them as input.
    cache_read = _count(usage.get("cache_read_input_tokens"))
    cache_write = _count(usage.get("cache_creation_input_tokens"))
    if prompt is not None and "total_tokens" not in usage:
        prompt += (cache_read or 0) + (cache_write or 0)
    if cached is None:
        cached = cache_read
    if prompt is not None:
        found["input_tokens"] = prompt
    if output is not None:
        found["output_tokens"] = output
    if cached is not None:
        found["cached_tokens"] = cached
    total = _count(usage.get("total_tokens"))
    if total is not None:
        found["total_tokens"] = total
    return found


class UsageMeter:
    """Reads token counts from a provider response as it streams past.

    Server-sent events are read line by line; other (JSON) responses are parsed
    once complete. Providers report usage cumulatively or once at the end, so
    the last value of each field wins.
    """

    def __init__(self, content_type: str) -> None:
        self.streamed = "text/event-stream" in content_type.lower()
        self._buffer = bytearray()
        self._fields: dict[str, int] = {}
        self._too_large = False

    def feed(self, chunk: bytes) -> None:
        if self._too_large:
            return
        self._buffer += chunk
        if not self.streamed:
            if len(self._buffer) > _MAX_JSON_RESPONSE:
                self._too_large = True
                self._buffer.clear()
            return
        while (end := self._buffer.find(b"\n")) >= 0:
            line = bytes(self._buffer[:end]).strip()
            del self._buffer[:end + 1]
            self._line(line)
        if len(self._buffer) > _MAX_SSE_LINE:
            self._buffer.clear()

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            self._absorb(json.loads(data))
        except ValueError:
            pass

    def _absorb(self, obj: Any) -> None:
        if isinstance(obj, list):  # Gemini streams a JSON array without alt=sse
            for item in obj:
                self._absorb(item)
            return
        if not isinstance(obj, dict):
            return
        candidates = [obj.get("usage"), obj.get("usageMetadata")]
        for nested in ("message", "response"):  # Anthropic message_start, Responses API events
            inner = obj.get(nested)
            if isinstance(inner, dict):
                candidates.append(inner.get("usage"))
        for usage in candidates:
            if isinstance(usage, dict):
                self._fields.update(_usage_fields(usage))

    def result(self) -> TokenCounts | None:
        """The counts, or ``None`` when the response reported no usage."""
        if not self.streamed and self._buffer and not self._too_large:
            try:
                self._absorb(json.loads(bytes(self._buffer)))
            except ValueError:
                pass
            self._buffer.clear()
        elif self.streamed and self._buffer:
            self._line(bytes(self._buffer).strip())
            self._buffer.clear()
        fields = self._fields
        if not fields:
            return None
        input_tokens = fields.get("input_tokens", 0)
        output_tokens = fields.get("output_tokens", 0)
        total = fields.get("total_tokens", input_tokens + output_tokens)
        return TokenCounts(input_tokens, output_tokens, fields.get("cached_tokens", 0), total)


def request_model(body: bytes | None, tail: str) -> str:
    """The model a provider request asks for: the JSON body's ``model``, or Gemini's path."""
    if body:
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            return data["model"][:200]
    match = _GEMINI_MODEL.search(tail)
    return match.group(1)[:200] if match else ""


# ── quotas ───────────────────────────────────────────────────────────


class QuotaError(ValueError):
    """An invalid quota setting (the message says which)."""


@dataclass(frozen=True)
class Limits:
    """Token limits; ``None`` means unlimited (or, in an override, the team default)."""

    daily: int | None = None
    monthly: int | None = None

    def as_json(self) -> dict[str, int | None]:
        return {"daily": self.daily, "monthly": self.monthly}


@dataclass(frozen=True)
class Quotas:
    default: Limits = Limits()
    users: dict[str, Limits] | None = None
    warn_percent: int = DEFAULT_WARN_PERCENT

    def for_user(self, username: str) -> Limits:
        """The user's limits: the override where set, else the team default (0 = unlimited)."""
        override = (self.users or {}).get(username, Limits())
        daily = override.daily if override.daily is not None else self.default.daily
        monthly = override.monthly if override.monthly is not None else self.default.monthly
        return Limits(daily or None, monthly or None)

    def as_json(self) -> dict[str, Any]:
        return {
            "default": self.default.as_json(),
            "users": {name: limits.as_json() for name, limits in sorted((self.users or {}).items())},
            "warn_percent": self.warn_percent,
        }


def _limit(value: Any, where: str, *, allow_zero: bool) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0 or (value == 0 and not allow_zero):
        raise QuotaError(f"{where} must be a whole number of tokens{' (0: unlimited)' if allow_zero else ''}, or null")
    return value


def _limits(value: Any, where: str, *, override: bool) -> Limits:
    if value is None:
        return Limits()
    if not isinstance(value, dict):
        raise QuotaError(f"{where} must be an object with daily and monthly")
    unknown = sorted(set(value) - set(PERIODS))
    if unknown:
        raise QuotaError(f"{where}: unknown key(s) {', '.join(unknown)}")
    daily, monthly = (_limit(value.get(period), f"{where}.{period}", allow_zero=True) for period in PERIODS)
    if override:
        return Limits(daily, monthly)  # null follows the team default; 0 is unlimited for this user
    return Limits(daily or None, monthly or None)  # null or 0: unlimited


def parse_quotas(data: Any, *, valid_user=None) -> Quotas:
    """Validate quota settings (the ``/server/admin/quotas`` body)."""
    if not isinstance(data, dict):
        raise QuotaError("quotas must be an object")
    unknown = sorted(set(data) - {"default", "users", "warn_percent"})
    if unknown:
        raise QuotaError(f"unknown key(s) {', '.join(unknown)}")
    default = _limits(data.get("default"), "default", override=False)
    users_raw = data.get("users") or {}
    if not isinstance(users_raw, dict):
        raise QuotaError("users must be an object of username: {daily, monthly}")
    users: dict[str, Limits] = {}
    for name, value in users_raw.items():
        if not isinstance(name, str) or (valid_user is not None and not valid_user(name)):
            raise QuotaError(f"users: {name!r} is not a valid username")
        limits = _limits(value, f"users.{name}", override=True)
        if limits != Limits():
            users[name] = limits
    warn = data.get("warn_percent", DEFAULT_WARN_PERCENT)
    if type(warn) is not int or not 1 <= warn <= 100:
        raise QuotaError("warn_percent must be a whole number from 1 to 100")
    return Quotas(default, users, warn)


# ── periods (server local time) ──────────────────────────────────────


def local_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if ts is None else ts))


def period_start(period: str, day: str) -> str:
    """First day of the period that contains ``day``."""
    return day if period == "daily" else day[:8] + "01"


def period_reset(period: str, day: str) -> str:
    """When the period that contains ``day`` ends, as a local ISO timestamp with offset."""
    current = date.fromisoformat(day)
    if period == "daily":
        nxt = current + timedelta(days=1)
    else:
        nxt = (current.replace(day=1) + timedelta(days=32)).replace(day=1)
    return datetime(nxt.year, nxt.month, nxt.day).astimezone().isoformat()


def quota_status(limits: Limits, used: dict[str, int], warn_percent: int, day: str) -> dict[str, Any]:
    """Per period: limit, used, remaining, warn/exceeded and when it resets."""
    status: dict[str, Any] = {}
    for period in PERIODS:
        limit = getattr(limits, period)
        spent = used.get(period, 0)
        status[period] = {
            "limit": limit,
            "used": spent,
            "remaining": None if limit is None else max(limit - spent, 0),
            "warn": limit is not None and spent * 100 >= limit * warn_percent,
            "exceeded": limit is not None and spent >= limit,
            "resets_at": period_reset(period, day),
        }
    return status
