"""Graceful gateway shutdown on SIGTERM/SIGINT (#172)."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from mira_engine.cli import gateway_shutdown, remote
from mira_engine.cli.engine_lease import read_lease
from mira_engine.cli.gateway_shutdown import GatewayShutdown, shutdown_signals
from mira_engine.cli.port_guard import port_busy

REPO_ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX signals are sent to a real subprocess")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in {"MIRA_SERVICE_MODE", "MIRA_LAUNCH_MODE", "MIRA_CONFIG_PATH", "MIRA_GATEWAY_TOKEN"}}
    env.update(
        MIRA_HOME=os.environ["MIRA_HOME"],
        MIRA_RUNTIME_DIR=os.environ["MIRA_RUNTIME_DIR"],
        HOME=str(tmp_path / "user-home"),
        PYTHONPATH=str(REPO_ROOT),
        PYTHONUNBUFFERED="1",
    )
    assert str(tmp_path) in env["MIRA_HOME"], "tests must never touch the real MIRA_HOME"
    return env


def _wait_for(predicate, timeout: float, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.1)
    return False


@pytest.fixture
def gateway(tmp_path):
    """Start a real `mira gateway` in a temp MIRA_HOME; always kill it afterwards."""
    home = Path(os.environ["MIRA_HOME"])
    port = _free_port()
    log = (tmp_path / "gateway.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mira_engine.cli.commands", "gateway", "--host", "127.0.0.1", "--port", str(port)],
        env=_isolated_env(tmp_path), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        healthy = _wait_for(lambda: remote._healthy(port) and read_lease(home) is not None, 60, proc)
        assert healthy, (tmp_path / "gateway.log").read_text(errors="replace")[-3000:]
        assert read_lease(home)["pid"] == proc.pid
        yield proc, port, home, tmp_path / "gateway.log"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        log.close()


def _open_ws(port: int, home: Path):
    import websocket

    token = (home / "runtime" / "gateway-token").read_text(encoding="utf-8").strip()
    return websocket.create_connection(f"ws://127.0.0.1:{port}/ws",
                                       header=[f"Authorization: Bearer {token}"], timeout=15,
                                       suppress_origin=True)


def _close_code(ws) -> int | None:
    import websocket

    while True:
        try:
            opcode, frame = ws.recv_data_frame(control_frame=True)
        except websocket.WebSocketConnectionClosedException:
            return None
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            return struct.unpack("!H", frame.data[:2])[0] if len(frame.data) >= 2 else None


# ── real gateway ─────────────────────────────────────────────────────


@posix_only
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_closes_clients_releases_lease_and_exits_zero(gateway, sig):
    proc, port, home, log = gateway
    ws = _open_ws(port, home)
    try:
        started = time.monotonic()
        os.kill(proc.pid, sig)
        assert _close_code(ws) == 1001  # going away
    finally:
        ws.close()
    assert proc.wait(timeout=15) == 0, log.read_text(errors="replace")[-3000:]
    assert time.monotonic() - started < gateway_shutdown.SHUTDOWN_TIMEOUT_S
    assert read_lease(home) is None
    assert not port_busy("127.0.0.1", port)  # TIME_WAIT aside, nothing listens any more


@posix_only
def test_remote_stop_sees_a_clean_shutdown(gateway, monkeypatch):
    proc, _port, home, log = gateway
    # Prove the engine released its own lease: stop must not need its fallback cleanup.
    monkeypatch.setattr(remote, "_remove_record_of", lambda _record: None)
    assert remote.stop() == f"stopped engine pid {proc.pid}"
    assert proc.wait(timeout=5) == 0, log.read_text(errors="replace")[-3000:]
    assert read_lease(home) is None
    assert remote.status()["running"] is False


# ── forced exits (real process, real lease, shutdown that hangs) ─────

_HANGING_ENGINE = textwrap.dedent(
    """
    import asyncio, sys, time
    from mira_engine.cli.engine_lease import acquire_engine_lease
    from mira_engine.cli.gateway_shutdown import GatewayShutdown

    lease = acquire_engine_lease(host="127.0.0.1", port=1, heartbeat=False)
    shutdown = GatewayShutdown(timeout_s=float(sys.argv[1]), on_force_exit=lease.release)

    async def main():
        stop = shutdown.bind(asyncio.get_running_loop())
        print("ready", flush=True)
        await stop.wait()
        print("stopping", flush=True)
        time.sleep(60)  # a shutdown step stuck in synchronous code

    shutdown.install()
    asyncio.run(main())
    """
)


def _start_hanging_engine(tmp_path: Path, timeout_s: float) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", _HANGING_ENGINE, str(timeout_s)],
                            env=_isolated_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    assert proc.stdout.readline().strip() == "ready", proc.stderr.read()
    assert read_lease(Path(os.environ["MIRA_HOME"]))["pid"] == proc.pid
    return proc


@posix_only
def test_second_signal_forces_immediate_exit_and_releases_lease(tmp_path):
    proc = _start_hanging_engine(tmp_path, timeout_s=30)
    try:
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.stdout.readline().strip() == "stopping"
        started = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=5) == 128 + signal.SIGTERM
        assert time.monotonic() - started < 5
        assert "again; exiting now" in proc.stderr.read()
        assert read_lease(Path(os.environ["MIRA_HOME"])) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@posix_only
def test_watchdog_forces_exit_when_shutdown_hangs(tmp_path):
    proc = _start_hanging_engine(tmp_path, timeout_s=1)
    try:
        os.kill(proc.pid, signal.SIGINT)
        assert proc.wait(timeout=10) == gateway_shutdown.SHUTDOWN_TIMEOUT_EXIT_CODE
        assert "did not shut down within 1s" in proc.stderr.read()
        assert read_lease(Path(os.environ["MIRA_HOME"])) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ── unit: the state machine and the Windows-safe path ────────────────


def _recorder():
    calls: list[tuple[str, int]] = []
    return calls, (lambda: calls.append(("release", 0))), (lambda code: calls.append(("exit", code)))


def test_first_signal_requests_shutdown_and_second_forces_exit():
    calls, release, exit_fn = _recorder()
    shutdown = GatewayShutdown(timeout_s=0, on_force_exit=release, exit_fn=exit_fn)

    async def scenario():
        event = shutdown.bind(asyncio.get_running_loop())
        shutdown.request(signal.SIGTERM)
        await asyncio.wait_for(event.wait(), 2)

    asyncio.run(scenario())
    assert shutdown.requested and calls == []
    shutdown.request(signal.SIGINT)
    assert calls == [("release", 0), ("exit", 128 + signal.SIGINT)]


def test_signal_before_the_loop_starts_is_not_lost():
    shutdown = GatewayShutdown(timeout_s=0, exit_fn=lambda _code: None)
    shutdown.request(signal.SIGTERM)

    async def scenario():
        return shutdown.bind(asyncio.get_running_loop()).is_set()

    assert asyncio.run(scenario()) is True


def test_watchdog_releases_lease_and_exits_with_timeout_code():
    calls, release, _ = _recorder()
    exited = threading.Event()

    def exit_fn(code):
        calls.append(("exit", code))
        exited.set()

    shutdown = GatewayShutdown(timeout_s=0.05, on_force_exit=release, exit_fn=exit_fn)
    shutdown.request(signal.SIGTERM)
    assert exited.wait(2)
    assert calls == [("release", 0), ("exit", gateway_shutdown.SHUTDOWN_TIMEOUT_EXIT_CODE)]


def test_force_exit_still_exits_when_release_fails():
    codes: list[int] = []

    def broken_release():
        raise OSError("disk gone")

    GatewayShutdown(on_force_exit=broken_release, exit_fn=codes.append).force_exit(143)
    assert codes == [143]


def test_install_uses_plain_signal_handlers_and_restores_them(monkeypatch):
    installed: dict[int, object] = {}

    def fake_signal(sig, handler):
        previous = installed.get(sig, signal.SIG_DFL)
        installed[sig] = handler
        return previous

    monkeypatch.setattr(gateway_shutdown.signal, "signal", fake_signal)
    shutdown = GatewayShutdown()
    shutdown.install()
    assert set(installed) == set(shutdown_signals())
    assert all(handler == shutdown._handle for handler in installed.values())
    shutdown.restore()
    assert all(handler == signal.SIG_DFL for handler in installed.values())


def test_windows_signal_set_and_no_loop_signal_api(monkeypatch):
    """Windows has no loop.add_signal_handler; WinSW stops the service with Ctrl+C (SIGINT)."""
    monkeypatch.setattr(gateway_shutdown.os, "name", "nt")
    monkeypatch.setattr(gateway_shutdown.signal, "SIGBREAK", 21, raising=False)
    assert 21 in shutdown_signals() and signal.SIGINT in shutdown_signals()

    installed: list[int] = []
    monkeypatch.setattr(gateway_shutdown.signal, "signal", lambda sig, _handler: installed.append(sig))

    class WindowsLoop:
        def add_signal_handler(self, *_args):  # what ProactorEventLoop does
            raise NotImplementedError

        def call_soon_threadsafe(self, callback, *args):
            callback(*args)

    calls, release, exit_fn = _recorder()
    shutdown = GatewayShutdown(timeout_s=0, on_force_exit=release, exit_fn=exit_fn)
    shutdown.install()
    assert installed == shutdown_signals()

    shutdown._loop = WindowsLoop()
    shutdown._handle(signal.SIGINT, None)
    assert shutdown.signum == signal.SIGINT
    shutdown._handle(21, None)
    assert calls == [("release", 0), ("exit", 128 + 21)]


def test_install_off_the_main_thread_is_harmless(monkeypatch):
    def refuse(_sig, _handler):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(gateway_shutdown.signal, "signal", refuse)
    GatewayShutdown().install()


def test_signal_after_the_loop_closed_does_not_raise():
    shutdown = GatewayShutdown(timeout_s=0, exit_fn=lambda _code: None)
    loop = asyncio.new_event_loop()
    shutdown.bind(loop)
    loop.close()
    shutdown.request(signal.SIGTERM)
    assert shutdown.requested


def test_run_bounded_moves_on_after_timeout_and_errors():
    async def hang():
        await asyncio.sleep(30)

    async def fail():
        raise RuntimeError("boom")

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await gateway_shutdown.run_bounded("hang", hang(), started + 0.2)
        await gateway_shutdown.run_bounded("fail", fail(), started + 5)
        return loop.time() - started

    assert asyncio.run(scenario()) < 2
