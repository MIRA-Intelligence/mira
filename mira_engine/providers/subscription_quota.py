"""Usage of subscription plans (ChatGPT, for OpenAI Codex) and team caps on it.

A ChatGPT plan has no token budget. OpenAI meters use against rolling windows
(a 5-hour one on most plans, and a weekly one) and reports how much of each is
used, as a percentage, in the headers of every Codex response
(``x-codex-primary-*`` and ``x-codex-secondary-*``). Which header holds which
window is not fixed, so windows are told apart by their length; a plan without
a 5-hour window reports none, and only its weekly window counts.

Mira Server administrators may cap how much of each window Mira uses
(``subscription_quota`` in the team layer, a percentage per window). The
percentage is the account's, so use outside Mira counts too. The cap is checked
before each request against the last reported usage, so a request in flight
can go past it by its own size. A window whose reset time has passed no longer
counts; one without a reset time counts for :data:`STALE_AFTER_S`, after which
one request may go out to learn the current usage.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

CODEX = "openai_codex"
FIVE_HOUR = "five_hour"
WEEKLY = "weekly"
WINDOWS = (FIVE_HOUR, WEEKLY)
_LABELS = {FIVE_HOUR: "5-hour", WEEKLY: "weekly"}
# Windows up to a day long are the short (5-hour) one; longer ones the weekly one.
_SHORT_WINDOW_MAX_MINUTES = 24 * 60
STALE_AFTER_S = 300.0


@dataclass(frozen=True)
class RateWindow:
    used_percent: float
    window_minutes: int | None = None
    resets_at: float | None = None  # epoch seconds

    def as_json(self) -> dict[str, Any]:
        return {"used_percent": self.used_percent, "window_minutes": self.window_minutes,
                "resets_at": self.resets_at}


@dataclass(frozen=True)
class PlanUsage:
    windows: dict[str, RateWindow] = field(default_factory=dict)
    account_id: str = ""
    updated_at: float = 0.0
    limit_reached: str | None = None  # x-codex-rate-limit-reached-type


_lock = threading.Lock()
_latest: dict[str, PlanUsage] = {}


def _number(headers: Any, name: str) -> float | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def parse_codex_headers(headers: Any, *, account_id: str = "", now: float | None = None) -> PlanUsage | None:
    """The plan usage a Codex response reports, or ``None`` when it reports none."""
    now = time.time() if now is None else now
    windows: dict[str, RateWindow] = {}
    for slot in ("primary", "secondary"):
        prefix = f"x-codex-{slot}"
        used = _number(headers, f"{prefix}-used-percent")
        if used is None:
            continue
        minutes = _number(headers, f"{prefix}-window-minutes")
        resets_at = _number(headers, f"{prefix}-reset-at")
        if resets_at is None and (after := _number(headers, f"{prefix}-reset-after-seconds")) is not None:
            resets_at = now + after
        if minutes is not None:
            name = FIVE_HOUR if minutes <= _SHORT_WINDOW_MAX_MINUTES else WEEKLY
        else:
            name = FIVE_HOUR if slot == "primary" else WEEKLY
        windows.setdefault(name, RateWindow(used, int(minutes) if minutes is not None else None, resets_at))
    if not windows:
        return None
    reached = headers.get("x-codex-rate-limit-reached-type") or None
    return PlanUsage(windows=windows, account_id=account_id, updated_at=now, limit_reached=reached)


def record(provider: str, usage: PlanUsage | None) -> None:
    if usage is None:
        return
    with _lock:
        _latest[provider] = usage


def latest(provider: str) -> PlanUsage | None:
    with _lock:
        return _latest.get(provider)


def clear() -> None:
    with _lock:
        _latest.clear()


def _caps(provider: str) -> dict[str, int]:
    from mira_engine.config import team_layer

    return dict(team_layer.get_team_layer().subscription_quota.get(provider) or {})


def _counts(window: RateWindow, usage: PlanUsage, now: float) -> bool:
    """Whether the reported usage still describes the window."""
    if window.resets_at is not None:
        return window.resets_at > now
    return now - usage.updated_at < STALE_AFTER_S


def _when(resets_at: float | None) -> str:
    if resets_at is None:
        return ""
    return f"; it resets at {time.strftime('%Y-%m-%d %H:%M', time.localtime(resets_at))}"


def refusal(provider: str, account_id: str = "", now: float | None = None) -> str | None:
    """Why a request must not go out (a window is at its team cap), else ``None``."""
    caps = _caps(provider)
    usage = latest(provider)
    if not caps or usage is None:
        return None
    if account_id and usage.account_id and usage.account_id != account_id:
        return None  # signed in to another account since
    now = time.time() if now is None else now
    for name in WINDOWS:
        cap, window = caps.get(name), usage.windows.get(name)
        if cap is None or window is None or not _counts(window, usage, now):
            continue
        if window.used_percent >= cap:
            return (f"The team's quota for the ChatGPT plan's {_LABELS[name]} window is used up "
                    f"({window.used_percent:g}% used, Mira may use up to {cap}%){_when(window.resets_at)}. "
                    "Ask your Mira Server administrators for more, or use another provider.")
    return None


def plan_limit_message(usage: PlanUsage | None) -> str:
    """Which plan window ran out, for a 429 from Codex ('' when the headers do not say)."""
    if usage is None:
        return ""
    for name in WINDOWS:
        window = usage.windows.get(name)
        if window is not None and window.used_percent >= 100:
            return f"The ChatGPT plan's {_LABELS[name]} limit is used up{_when(window.resets_at)}."
    return ""


def status(now: float | None = None) -> dict[str, Any]:
    """Each subscription provider's last reported usage and the team caps on it."""
    from mira_engine.config import team_layer

    now = time.time() if now is None else now
    caps_by_provider = team_layer.get_team_layer().subscription_quota
    with _lock:
        known = dict(_latest)
    providers: dict[str, Any] = {}
    for provider in sorted({*known, *caps_by_provider}):
        caps = caps_by_provider.get(provider) or {}
        usage = known.get(provider)
        windows: dict[str, Any] = {}
        for name in WINDOWS:
            window = usage.windows.get(name) if usage else None
            if window is None:
                continue
            current = _counts(window, usage, now)
            cap = caps.get(name)
            windows[name] = {**window.as_json(), "current": current, "limit_percent": cap,
                             "exceeded": current and cap is not None and window.used_percent >= cap}
        providers[provider] = {
            "updated_at": usage.updated_at if usage else None,
            "limit_reached": usage.limit_reached if usage else None,
            "windows": windows,
            "limits": {name: caps.get(name) for name in WINDOWS},
        }
    return {"providers": providers}
