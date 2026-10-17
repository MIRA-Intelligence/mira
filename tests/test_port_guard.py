"""The gateway never stops a port holder it cannot prove is a stale engine for its MIRA_HOME (#172)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest
from typer.testing import CliRunner

from mira_engine.cli import commands, port_guard, remote
from mira_engine.cli.commands import app
from mira_engine.cli.engine_lease import read_lease
from mira_engine.cli.port_guard import (
    PORT_IN_USE_EXIT_CODE,
    PortInUseError,
    free_port_from_stale_engine,
)

# conftest stubs this out for every other gateway test; these tests need the real one.
_REAL_PORT_CHECK = commands._free_gateway_port_or_exit

REPO_ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX process and socket semantics")

# Holds a port. Run as `python <path>/mira gateway --port N` it looks like a Mira engine.
_HOLDER = textwrap.dedent(
    """
    import socket, sys, time
    port = int(sys.argv[sys.argv.index("--port") + 1])
    if "--lease" in sys.argv:
        from mira_engine.cli.engine_lease import acquire_engine_lease
        lease = acquire_engine_lease(host="127.0.0.1", port=port, heartbeat=False)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen()
    print("ready", flush=True)
    time.sleep(120)
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def holders(tmp_path):
    """Start port holders; kill any that are still running afterwards."""
    started: list[subprocess.Popen] = []
    fake_mira = tmp_path / "bin" / "mira"
    fake_mira.parent.mkdir()
    fake_mira.write_text(_HOLDER, encoding="utf-8")

    def start(port: int, *, mira: bool = False, home: Path | None = None, lease: bool = False) -> subprocess.Popen:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "HOME": str(tmp_path / "user-home")}
        if home is not None:
            env["MIRA_HOME"] = str(home)
        assert str(tmp_path) in env["MIRA_HOME"], "tests must never touch the real MIRA_HOME"
        command = ([sys.executable, str(fake_mira), "gateway"] if mira else [sys.executable, "-c", _HOLDER])
        command += ["--port", str(port)] + (["--lease"] if lease else [])
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        started.append(proc)
        assert proc.stdout.readline().strip() == "ready", proc.stderr.read()
        return proc

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _home() -> Path:
    return Path(os.environ["MIRA_HOME"])


# ── refusals ─────────────────────────────────────────────────────────


@posix_only
def test_gateway_refuses_a_port_held_by_an_unrelated_process(holders, monkeypatch):
    monkeypatch.setattr(commands, "_free_gateway_port_or_exit", _REAL_PORT_CHECK)
    port = _free_port()
    holder = holders(port)

    result = CliRunner().invoke(app, ["gateway", "--host", "127.0.0.1", "--port", str(port)])

    assert result.exit_code == PORT_IN_USE_EXIT_CODE
    assert f"port {port} on 127.0.0.1 is already in use by pid {holder.pid}" in result.stderr
    assert "which is not a Mira engine" in result.stderr
    assert "--port" in result.stderr
    assert holder.poll() is None, "an unrelated process must never be signalled"


@posix_only
def test_engine_for_another_home_is_left_alone(holders, tmp_path):
    port = _free_port()
    holder = holders(port, mira=True, home=tmp_path / "other-home")

    with pytest.raises(PortInUseError, match="a Mira engine for another MIRA_HOME"):
        free_port_from_stale_engine("127.0.0.1", port)
    assert holder.poll() is None


@posix_only
def test_live_lease_holder_is_left_alone(holders):
    port = _free_port()
    holder = holders(port, mira=True, home=_home(), lease=True)
    assert read_lease(_home())["pid"] == holder.pid

    with pytest.raises(PortInUseError, match="the running Mira engine"):
        free_port_from_stale_engine("127.0.0.1", port)
    assert holder.poll() is None


@posix_only
def test_process_of_another_user_is_never_signalled(holders, monkeypatch):
    port = _free_port()
    holder = holders(port, mira=True, home=_home())
    monkeypatch.setattr(port_guard, "_same_user", lambda _proc: False)
    monkeypatch.setattr(port_guard, "_terminate", lambda _proc: pytest.fail("must not signal"))

    with pytest.raises(PortInUseError, match="owned by another user"):
        free_port_from_stale_engine("127.0.0.1", port)
    assert holder.poll() is None


def test_invisible_listener_is_reported_as_another_users(monkeypatch):
    monkeypatch.setattr(port_guard, "port_busy", lambda *_args: True)
    monkeypatch.setattr(port_guard, "port_listeners", lambda *_args: [None])
    with pytest.raises(PortInUseError, match="owned by another user"):
        free_port_from_stale_engine("127.0.0.1", 1)


def test_no_visible_listener_but_busy_port_is_reported(monkeypatch):
    monkeypatch.setattr(port_guard, "port_busy", lambda *_args: True)
    monkeypatch.setattr(port_guard, "port_listeners", lambda *_args: [])
    with pytest.raises(PortInUseError, match="cannot see"):
        free_port_from_stale_engine("127.0.0.1", 1)


def test_holders_are_checked_before_any_is_signalled(monkeypatch):
    """A stale engine sharing the port with an unrelated process is not stopped either."""
    stale = MagicMock(pid=11)
    monkeypatch.setattr(port_guard, "port_busy", lambda *_args: True)
    monkeypatch.setattr(port_guard, "port_listeners", lambda *_args: [11, 12])
    monkeypatch.setattr(port_guard, "classify", lambda pid, _home: stale if pid == 11 else "pid 12 (nginx)")
    monkeypatch.setattr(port_guard, "_terminate", lambda _proc: pytest.fail("must not signal"))
    with pytest.raises(PortInUseError, match="nginx"):
        free_port_from_stale_engine("127.0.0.1", 1)


# ── recovery ─────────────────────────────────────────────────────────


@posix_only
def test_stale_engine_for_the_same_home_is_stopped(holders):
    port = _free_port()
    holder = holders(port, mira=True, home=_home())

    free_port_from_stale_engine("127.0.0.1", port)

    assert not port_guard._still_running(psutil.Process(holder.pid))
    assert not port_guard.port_busy("127.0.0.1", port)


@posix_only
def test_gateway_recovers_its_port_from_a_stale_engine(holders, tmp_path):
    port = _free_port()
    stale = holders(port, mira=True, home=_home())
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "HOME": str(tmp_path / "user-home")}
    for key in ("MIRA_SERVICE_MODE", "MIRA_LAUNCH_MODE", "MIRA_CONFIG_PATH"):
        env.pop(key, None)
    log = (tmp_path / "gateway.log").open("wb")
    gateway = subprocess.Popen(
        [sys.executable, "-m", "mira_engine.cli.commands", "gateway", "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and gateway.poll() is None and not remote._healthy(port):
            time.sleep(0.1)
        output = (tmp_path / "gateway.log").read_text(errors="replace")
        assert remote._healthy(port), output[-3000:]
        assert stale.wait(timeout=5) == -signal.SIGTERM  # reaped by us, its parent
        assert read_lease(_home())["pid"] == gateway.pid
        assert f"Stopping stale Mira engine pid {stale.pid}" in output
        os.kill(gateway.pid, signal.SIGTERM)
        assert gateway.wait(timeout=15) == 0
    finally:
        if gateway.poll() is None:
            gateway.kill()
            gateway.wait(timeout=10)
        log.close()


def test_free_port_is_a_no_op(monkeypatch):
    monkeypatch.setattr(port_guard, "port_listeners", lambda *_args: pytest.fail("no lookup needed"))
    free_port_from_stale_engine("127.0.0.1", _free_port())


# ── helpers and the Windows path ─────────────────────────────────────


@pytest.mark.parametrize(
    ("bind_host", "listen_ip", "expected"),
    [
        ("127.0.0.1", "127.0.0.1", True),
        ("127.0.0.1", "0.0.0.0", True),
        ("0.0.0.0", "192.0.2.5", True),
        ("localhost", "::1", True),
        ("127.0.0.1", "192.0.2.5", False),
    ],
)
def test_address_overlap(bind_host, listen_ip, expected):
    assert port_guard._overlaps(bind_host, listen_ip) is expected


def test_windows_terminates_instead_of_sending_sigterm(monkeypatch):
    monkeypatch.setattr(port_guard.os, "name", "nt")
    proc = MagicMock()
    port_guard._terminate(proc)
    proc.terminate.assert_called_once()
    proc.send_signal.assert_not_called()


def test_windows_compares_owner_by_username(monkeypatch):
    monkeypatch.delattr(port_guard.os, "getuid", raising=False)
    me = psutil.Process().username()
    assert port_guard._same_user(MagicMock(username=lambda: me)) is True
    assert port_guard._same_user(MagicMock(username=lambda: me + "-other")) is False


def test_listener_lookup_falls_back_to_lsof_without_root(monkeypatch):
    def denied(**_kwargs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(port_guard.psutil, "net_connections", denied)
    monkeypatch.setattr(port_guard, "_lsof_listeners", lambda port: [4242] if port == 9 else [])
    assert port_guard.port_listeners("127.0.0.1", 9) == [4242]
