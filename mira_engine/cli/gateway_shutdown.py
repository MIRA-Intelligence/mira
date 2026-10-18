"""Graceful shutdown for ``mira gateway`` on SIGTERM and SIGINT (SIGBREAK on Windows).

The first signal asks the event loop to stop (channels, cron, heartbeat, MCP
children, then the engine lease) and arms a watchdog. If shutdown is not done
when the watchdog fires, or a second signal arrives, the process releases its
lease and exits at once.

Handlers use ``signal.signal`` rather than ``loop.add_signal_handler``. The
latter does not exist on Windows, where the desktop engine runs and WinSW stops
the service with Ctrl+C (SIGINT). A plain handler also still runs while the
event loop is stuck in synchronous code, which is when a second signal matters.
Signal handlers never log through loguru (its lock is not reentrant); they only
set state, wake the loop, or write to stderr directly before a forced exit.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Callable
from typing import Any

SHUTDOWN_TIMEOUT_S = 10.0
# Exit code when the watchdog forces an exit; a second signal exits with 128 + signum.
SHUTDOWN_TIMEOUT_EXIT_CODE = 1


def shutdown_signals() -> list[signal.Signals]:
    names = ["SIGINT", "SIGTERM"]
    if os.name == "nt":
        names.append("SIGBREAK")  # CTRL_BREAK_EVENT, sent to detached process groups
    return [getattr(signal, name) for name in names if hasattr(signal, name)]


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def _write_stderr(text: str) -> None:
    try:
        os.write(2, text.encode("utf-8", errors="replace"))
    except OSError:
        pass


class GatewayShutdown:
    """Turns shutdown signals into a bounded, graceful gateway stop."""

    def __init__(
        self,
        *,
        timeout_s: float = SHUTDOWN_TIMEOUT_S,
        on_force_exit: Callable[[], None] | None = None,
        exit_fn: Callable[[int], Any] = os._exit,
    ) -> None:
        self.timeout_s = timeout_s
        self.signum: int | None = None
        self._on_force_exit = on_force_exit
        self._exit = exit_fn
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._watchdog: threading.Timer | None = None
        self._previous: dict[int, Any] = {}

    @property
    def requested(self) -> bool:
        return self.signum is not None

    # ── handlers ─────────────────────────────────────────────────────

    def install(self) -> None:
        """Install handlers; call from the main thread before the event loop starts."""
        for sig in shutdown_signals():
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError):  # not the main thread, or not supported here
                continue

    def restore(self) -> None:
        for sig, previous in self._previous.items():
            try:
                signal.signal(sig, previous if previous is not None else signal.SIG_DFL)
            except (ValueError, OSError, TypeError):
                pass
        self._previous.clear()

    def _handle(self, signum: int, _frame: Any) -> None:
        self.request(signum)

    # ── event loop side ──────────────────────────────────────────────

    def bind(self, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
        """Return the event the gateway waits on; set already if a signal came first."""
        self._loop = loop
        self._event = asyncio.Event()
        if self.requested:
            self._event.set()
        return self._event

    def _notify_loop(self) -> None:
        if self._event is not None:
            self._event.set()
        from loguru import logger

        logger.info("Received {}; shutting down (up to {:.0f}s; send it again to force exit)",
                    _signal_name(self.signum or 0), self.timeout_s)

    # ── shutdown logic ───────────────────────────────────────────────

    def request(self, signum: int) -> None:
        if self.signum is not None:
            _write_stderr(f"Received {_signal_name(signum)} again; exiting now.\n")
            self.force_exit(128 + int(signum))
            return
        self.signum = int(signum)
        self._arm_watchdog()
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._notify_loop)
        except RuntimeError:  # the loop already closed; the process is on its way out
            pass

    def _arm_watchdog(self) -> None:
        if self.timeout_s <= 0:
            return
        timer = threading.Timer(self.timeout_s, self._on_timeout)
        timer.daemon = True
        timer.name = "mira-gateway-shutdown-watchdog"
        self._watchdog = timer
        timer.start()

    def _on_timeout(self) -> None:
        _write_stderr(f"Gateway did not shut down within {self.timeout_s:.0f}s; forcing exit.\n")
        self.force_exit(SHUTDOWN_TIMEOUT_EXIT_CODE)

    def cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def force_exit(self, code: int) -> None:
        """Release the lease (fast, file-only) and leave without further cleanup."""
        if self._on_force_exit is not None:
            try:
                self._on_force_exit()
            except BaseException:  # noqa: BLE001 - nothing may stop a forced exit
                pass
        self._exit(code)


async def run_bounded(name: str, step: Any, deadline: float) -> None:
    """Await one shutdown step until ``deadline`` (loop time); log and move on when it fails."""
    from loguru import logger

    remaining = max(0.1, deadline - asyncio.get_running_loop().time())
    try:
        await asyncio.wait_for(step, remaining)
    except asyncio.TimeoutError:
        logger.warning("Gateway shutdown: {} did not finish within {:.1f}s", name, remaining)
    except Exception:  # noqa: BLE001 - one failing step must not skip the others
        logger.exception("Gateway shutdown: {} failed", name)
