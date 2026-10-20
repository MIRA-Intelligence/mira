"""Login rate limiting per (username, client IP)."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

_MAX_KEYS = 10_000


class LoginRateLimiter:
    """Allow ``max_attempts`` failed logins per key within ``window`` seconds."""

    def __init__(self, max_attempts: int, window: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_attempts = max_attempts
        self.window = window
        self._clock = clock
        self._failures: dict[tuple[str, str], deque[float]] = {}

    @staticmethod
    def key(username: str, client_ip: str | None) -> tuple[str, str]:
        return username.strip().lower(), client_ip or "?"

    def _prune(self, key: tuple[str, str], now: float) -> deque[float] | None:
        failures = self._failures.get(key)
        if failures is None:
            return None
        while failures and now - failures[0] >= self.window:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return None
        return failures

    def retry_after(self, key: tuple[str, str]) -> float:
        """Seconds until another attempt is allowed; 0 when allowed now."""
        now = self._clock()
        failures = self._prune(key, now)
        if failures is None or len(failures) < self.max_attempts:
            return 0.0
        return max(self.window - (now - failures[0]), 0.0)

    def record_failure(self, key: tuple[str, str]) -> None:
        now = self._clock()
        if len(self._failures) >= _MAX_KEYS and key not in self._failures:
            for other in list(self._failures):
                self._prune(other, now)
            if len(self._failures) >= _MAX_KEYS:
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(key, deque()).append(now)

    def reset(self, key: tuple[str, str]) -> None:
        self._failures.pop(key, None)
