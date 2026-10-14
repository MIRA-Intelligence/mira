"""Decide what to do when the gateway port is already taken.

The gateway stops a process on its port only when it can prove that process is
a leftover Mira engine for the same MIRA_HOME: owned by the same user, running a
Mira gateway command, tied to this home, and not the engine that holds the
lease. Anything else is reported by pid and name and left alone.
"""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
from loguru import logger

PORT_IN_USE_EXIT_CODE = 4
# Long enough for a stale engine to run its own graceful shutdown (10 s).
STALE_STOP_TIMEOUT_S = 15.0
_UNSPECIFIED = {"", "0.0.0.0", "::"}
_GONE = object()


class PortInUseError(RuntimeError):
    """The port is held by a process the gateway must not stop."""

    def __init__(self, host: str, port: int, holder: str) -> None:
        self.host = host
        self.port = port
        self.holder = holder
        super().__init__(f"port {port} on {host} is already in use by {holder}")


# ── discovery ────────────────────────────────────────────────────────


def _host_ips(host: str) -> set[str]:
    if host in _UNSPECIFIED:
        return {host}
    if host == "localhost":
        return {"127.0.0.1", "::1"}
    try:
        return {str(ipaddress.ip_address(host.strip("[]")))}
    except ValueError:
        try:
            return {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        except OSError:
            return {host}


def _overlaps(bind_host: str, listen_ip: str) -> bool:
    """Whether a listener on ``listen_ip`` stops us binding ``bind_host`` (conservatively)."""
    mine = _host_ips(bind_host)
    return bool(mine & _UNSPECIFIED) or listen_ip in _UNSPECIFIED or listen_ip in mine


def _lsof_listeners(port: int) -> list[int | None]:
    try:
        result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int | None] = []
    for token in result.stdout.split():
        if token.isdigit() and int(token) not in pids:
            pids.append(int(token))
    return pids


def port_listeners(host: str, port: int) -> list[int | None]:
    """PIDs listening on ``port`` in a way that conflicts with ``host``.

    ``None`` stands for a listener whose owner this user cannot see.
    """
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, PermissionError):
        # macOS needs root for a system-wide view; lsof still sees this user's processes.
        return _lsof_listeners(port)
    except OSError:
        return []
    pids: list[int | None] = []
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port != port:
            continue
        if _overlaps(host, conn.laddr.ip) and conn.pid not in pids:
            pids.append(conn.pid)
    return pids


def port_busy(host: str, port: int) -> bool:
    """Whether binding ``host:port`` the way the gateway does would fail, or something answers there."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            # Same as aiohttp's reuse_address: ignore TIME_WAIT leftovers, not live listeners.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    probe = "127.0.0.1" if host in _UNSPECIFIED else host
    with socket.socket(family if probe == host else socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            return sock.connect_ex((probe, port)) == 0
        except OSError:
            return False


# ── identification ───────────────────────────────────────────────────


def _same_user(proc: psutil.Process) -> bool:
    if hasattr(os, "getuid"):
        return proc.uids().real == os.getuid()
    return proc.username() == psutil.Process().username()


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _home_from_environ(env: dict[str, str]) -> Path | None:
    if env.get("MIRA_HOME"):
        return _resolve(env["MIRA_HOME"])
    user_home = env.get("HOME") or env.get("USERPROFILE")
    return _resolve(Path(user_home) / ".mira") if user_home else None


def _listed_in_discovery(proc: psutil.Process, home: Path) -> bool:
    """Whether ``<home>/runtime/gateways.json`` records this exact process."""
    try:
        data = json.loads((home / "runtime" / "gateways.json").read_text(encoding="utf-8"))
        instances = data.get("instances", {}).values()
    except (OSError, ValueError, AttributeError):
        return False
    for record in instances:
        if not isinstance(record, dict) or record.get("pid") != proc.pid:
            continue
        created = record.get("create_time")
        if isinstance(created, (int, float)) and abs(proc.create_time() - created) < 0.01:
            return True
    return False


def _serves_home(proc: psutil.Process, home: Path) -> bool:
    if _listed_in_discovery(proc, home):
        return True
    try:
        env_home = _home_from_environ(proc.environ())
    except (psutil.AccessDenied, OSError):
        return False
    return env_home == home


def _is_lease_holder(proc: psutil.Process, home: Path) -> bool:
    from mira_engine.cli.engine_lease import _hostname, holder_alive, read_lease

    record = read_lease(home)
    return bool(record) and record.get("pid") == proc.pid and record.get("hostname") == _hostname() \
        and holder_alive(record)


def _short_command(proc: psutil.Process) -> str:
    try:
        argv = proc.cmdline()
        command = " ".join([Path(argv[0]).name, *argv[1:]]) if argv else ""
    except psutil.Error:
        command = ""
    try:
        name = proc.name()
    except psutil.Error:
        name = "?"
    if len(command) > 120:
        command = command[:117] + "..."
    return f"{name}: {command}" if command else name


def classify(pid: int, home: Path) -> Any:
    """Return the process when it is a provably stale engine for ``home``, ``_GONE``, or a reason string."""
    from mira_engine.cli.gateway_lifecycle import is_gateway_command

    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return _GONE
        label = f"pid {pid} ({_short_command(proc)})"
        if not _same_user(proc):
            return f"pid {pid}, owned by another user"
        if not is_gateway_command(proc.cmdline()):
            return f"{label}, which is not a Mira engine"
        if not _serves_home(proc, home):
            return f"a Mira engine for another MIRA_HOME ({label})"
        if _is_lease_holder(proc, home):
            return f"the running Mira engine for {home} ({label})"
        return proc
    except psutil.NoSuchProcess:
        return _GONE
    except psutil.AccessDenied:
        return f"pid {pid}, which this user is not allowed to inspect"


# ── action ───────────────────────────────────────────────────────────


def _still_running(proc: psutil.Process) -> bool:
    # A zombie no longer holds sockets; its parent (not us) reaps it.
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _terminate(proc: psutil.Process) -> None:
    # psutil re-checks the PID's creation time, so a reused PID is never signalled.
    if os.name == "nt":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGTERM)


def free_port_from_stale_engine(
    host: str,
    port: int,
    *,
    home: Path | None = None,
    timeout_s: float = STALE_STOP_TIMEOUT_S,
) -> None:
    """Make ``host:port`` bindable, stopping only a provably stale engine for ``home``.

    Raises :class:`PortInUseError` naming the holder when anything else has the port.
    """
    if not port_busy(host, port):
        return
    if home is None:
        from mira_engine.config.loader import get_home_dir

        home = get_home_dir()
    home = _resolve(home)

    own = os.getpid()
    stale: list[psutil.Process] = []
    for pid in port_listeners(host, port):
        if pid == own:
            continue
        if pid is None:
            raise PortInUseError(host, port, "a process owned by another user")
        verdict = classify(pid, home)
        if verdict is _GONE:
            continue
        if isinstance(verdict, str):
            raise PortInUseError(host, port, verdict)
        stale.append(verdict)

    if not stale:
        if port_busy(host, port):
            raise PortInUseError(host, port, "a process this user cannot see")
        return

    for proc in stale:
        logger.warning("Stopping stale Mira engine pid {} for {} on port {}", proc.pid, home, port)
        try:
            _terminate(proc)
        except psutil.NoSuchProcess:
            pass
    deadline = time.monotonic() + timeout_s
    while (alive := [proc for proc in stale if _still_running(proc)]) and time.monotonic() < deadline:
        time.sleep(0.1)
    if alive:
        pids = ", ".join(str(proc.pid) for proc in alive)
        raise PortInUseError(host, port, f"a stale Mira engine (pid {pids}) that did not exit "
                                         f"within {timeout_s:.0f}s")
