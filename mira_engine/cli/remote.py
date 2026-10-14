"""Remote-SSH helpers: start, attach to, inspect and stop this user's engine.

The desktop app runs these over SSH, for example
``ssh host -- sh -lc 'mira remote attach --json'``, then forwards the reported
port with ``ssh -L``. With ``--json`` stdout carries exactly one JSON line and
everything else goes to stderr, so the client can parse it reliably.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil
import typer

PROTOCOL = 1
DEFAULT_PORT = 18790
HEALTH_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 15.0
LAUNCH_MODE_ENV = "MIRA_LAUNCH_MODE"
REMOTE_LAUNCH_MODE = "remote-ssh"
# Engines this command may stop; service-managed ones belong to their supervisor.
_STOPPABLE_MODES = {"standalone", REMOTE_LAUNCH_MODE}
# Bind addresses whose engine is reachable through an ssh -L to 127.0.0.1.
_LOOPBACK_REACHABLE = {"127.0.0.1", "localhost", "0.0.0.0", "::", "::1", "[::]", "[::1]"}

remote_app = typer.Typer(help="Remote-SSH helpers for the desktop app")


class RemoteError(Exception):
    """A failure reported to the client as ``{"ok": false, "error": {...}}``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── helpers ──────────────────────────────────────────────────────────


def _home() -> Path:
    from mira_engine.config.loader import get_home_dir

    return get_home_dir().expanduser().resolve(strict=False)


def _token() -> str:
    from mira_engine.config.loader import load_config
    from mira_engine.security.gateway_auth import resolve_gateway_token

    try:
        configured = load_config().gateway.auth_token
    except Exception as exc:
        raise RemoteError("config_invalid", f"cannot load the Mira config: {exc}") from exc
    token = resolve_gateway_token(configured)
    if not token:
        raise RemoteError("config_invalid", "no gateway token is configured")
    return token


def _http_status(port: int, path: str, token: str | None = None, timeout: float = 2.0) -> int | None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _healthy(port: int) -> bool:
    status = _http_status(port, "/health")
    return status is not None and 200 <= status < 300


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _pick_port() -> int:
    if _port_free(DEFAULT_PORT):
        return DEFAULT_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _live_record() -> dict[str, Any] | None:
    from mira_engine.cli.engine_lease import holder_alive, read_lease

    record = read_lease(_home())
    return record if holder_alive(record) else None


def _success(record: dict[str, Any], token: str | None, *, started: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol": PROTOCOL,
        "ok": True,
        "port": record.get("port"),
        "version": record.get("version"),
        "pid": record.get("pid"),
        "mira_home": str(_home()),
        "started": started,
    }
    if token is not None:
        payload["token"] = token
    return payload


def _ensure_local(record: dict[str, Any]) -> None:
    from mira_engine.cli.engine_lease import _hostname

    if record.get("hostname") != _hostname():
        raise RemoteError(
            "lease_conflict",
            f"an engine for {_home()} is running on host {record.get('hostname')} "
            f"(pid {record.get('pid')}); stop it there first",
        )


def _check_attachable(record: dict[str, Any], token: str) -> None:
    """Verify a running engine can be reached through the tunnel with this token."""
    _ensure_local(record)
    host = str(record.get("host") or "")
    if host not in _LOOPBACK_REACHABLE:
        raise RemoteError("config_invalid",
                          f"the running engine listens on {host}, which an SSH tunnel to 127.0.0.1 cannot reach")
    port = record.get("port")
    if not isinstance(port, int):
        raise RemoteError("internal", "the engine lease has no port")
    if _http_status(port, "/api/config", token) == 401:
        raise RemoteError("config_invalid",
                          "the running engine rejected the local gateway token "
                          "(was it started with a different MIRA_GATEWAY_TOKEN?)")


def _wait_healthy(port: int, deadline: float, proc: subprocess.Popen | None = None) -> bool:
    while time.monotonic() < deadline:
        if _healthy(port):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.2)
    return False


def _spawned_by(pid: Any, parent_pid: int) -> bool:
    """Whether ``pid`` is the spawned process or its child (PyInstaller onefile bootloader)."""
    if pid == parent_pid:
        return True
    try:
        return isinstance(pid, int) and psutil.Process(pid).ppid() == parent_pid
    except psutil.Error:
        return False


def _log_path() -> Path:
    from mira_engine.config.paths import get_logs_dir

    return get_logs_dir() / "remote-gateway.log"


def _spawn_gateway(port: int) -> subprocess.Popen:
    """Start ``mira gateway`` detached from this SSH session."""
    from mira_engine.cli.agent_service import _gateway_service_args, _independent_subprocess_env

    env = _independent_subprocess_env(**{LAUNCH_MODE_ENV: REMOTE_LAUNCH_MODE, "PYTHONUNBUFFERED": "1"})
    env.pop("MIRA_SERVICE_MODE", None)
    log = open(_log_path(), "ab")  # noqa: SIM115 - handed to the child process
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": env,
        "cwd": str(_home()),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        # A new session: closing the SSH connection must not SIGHUP the engine.
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(_gateway_service_args("127.0.0.1", port), **kwargs)
    finally:
        log.close()


def _log_tail(limit: int = 2000) -> str:
    try:
        data = _log_path().read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


# ── operations ───────────────────────────────────────────────────────


def attach(timeout_s: float = HEALTH_TIMEOUT_S) -> dict[str, Any]:
    """Reuse this user's running engine or start one bound to loopback."""
    from mira_engine.cli.engine_lease import LEASE_CONFLICT_EXIT_CODE
    from mira_engine.cli.port_guard import PORT_IN_USE_EXIT_CODE

    token = _token()
    deadline = time.monotonic() + timeout_s
    for attempt in range(3):
        record = _live_record()
        if record is not None:
            _check_attachable(record, token)
            if not _wait_healthy(record["port"], deadline):
                raise RemoteError("health_timeout",
                                  f"the running engine (pid {record.get('pid')}) did not become healthy")
            return _success(record, token, started=False)

        port = _pick_port()
        proc = _spawn_gateway(port)
        if _wait_healthy(port, deadline, proc):
            record = _live_record()
            if record is None or not _spawned_by(record.get("pid"), proc.pid):
                # Something else answered on the port; never hand out a foreign engine.
                raise RemoteError("start_failed", f"port {port} is served by an unexpected process")
            _check_attachable(record, token)
            return _success(record, token, started=True)
        code = proc.poll()
        if code is None:
            raise RemoteError("health_timeout",
                              f"the engine (pid {proc.pid}) did not become healthy within {timeout_s:.0f}s; "
                              f"see {_log_path()}")
        if code in (LEASE_CONFLICT_EXIT_CODE, PORT_IN_USE_EXIT_CODE) or not _port_free(port):
            # Lost a race to another engine or for the port; look again. The gateway
            # never stops an unrelated port holder, so picking another port is the fix.
            continue
        tail = _log_tail()
        raise RemoteError("start_failed",
                          f"the engine exited with code {code}" + (f": {tail.splitlines()[-1]}" if tail else ""))
    raise RemoteError("start_failed", "could not start or attach to an engine after several attempts")


def status() -> dict[str, Any]:
    from mira_engine.cli.engine_lease import _hostname

    record = _live_record()
    if record is None:
        return {"protocol": PROTOCOL, "ok": True, "running": False, "mira_home": str(_home())}
    local = record.get("hostname") == _hostname()
    payload = _success(record, _token() if local else None, started=False)
    payload.update(running=True, hostname=record.get("hostname"))
    return payload


def stop(timeout_s: float = STOP_TIMEOUT_S) -> str:
    from mira_engine.cli.gateway_lifecycle import is_gateway_command

    record = _live_record()
    if record is None:
        return "no engine is running"
    _ensure_local(record)
    mode = record.get("launch_mode") or "standalone"
    if mode not in _STOPPABLE_MODES:
        raise RemoteError("lease_conflict",
                          f"the engine is managed by the {mode} service; use `mira-engine stop`")
    pid = record["pid"]
    try:
        proc = psutil.Process(pid)
        if not is_gateway_command(proc.cmdline()):
            raise RemoteError("internal", f"pid {pid} is not a Mira gateway; refusing to signal it")
        if os.name == "nt":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=timeout_s)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired as exc:
        raise RemoteError("internal", f"the engine (pid {pid}) did not stop within {timeout_s:.0f}s") from exc
    except psutil.AccessDenied as exc:
        raise RemoteError("internal", f"not allowed to stop pid {pid}") from exc
    _remove_record_of(record)
    return f"stopped engine pid {pid}"


def _remove_record_of(record: dict[str, Any]) -> None:
    """The gateway releases its lease on SIGTERM; drop the record if it crashed or was killed instead."""
    from mira_engine.cli.engine_lease import lease_path, read_lease

    current = read_lease(_home())
    if current and current.get("instance_id") == record.get("instance_id"):
        lease_path(_home()).unlink(missing_ok=True)


# ── CLI ──────────────────────────────────────────────────────────────


def _failure(exc: RemoteError) -> dict[str, Any]:
    return {"protocol": PROTOCOL, "ok": False, "error": {"code": exc.code, "message": exc.message}}


def _run(action, *, as_json: bool, render) -> None:
    """Run ``action`` keeping stdout clean, then print its result."""
    out = sys.stdout
    try:
        # Anything printed while working (imports, config warnings) goes to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            result = action()
    except RemoteError as exc:
        payload, ok = _failure(exc), False
    except Exception as exc:  # noqa: BLE001 - the client must always get one JSON line
        payload, ok = _failure(RemoteError("internal", f"{type(exc).__name__}: {exc}")), False
    else:
        payload, ok = result, True
    if as_json:
        out.write(json.dumps(payload, ensure_ascii=False) + "\n")
        out.flush()
    else:
        typer.echo(render(payload) if ok else f"Error: {payload['error']['message']}", err=not ok)
    if not ok:
        raise typer.Exit(1)


def setup() -> dict[str, Any]:
    from mira_engine import __version__
    from mira_engine.cli import launcher

    try:
        path = launcher.setup()
    except launcher.LauncherError as exc:
        raise RemoteError(exc.code, exc.message) from exc
    return {"protocol": PROTOCOL, "ok": True, "launcher": str(path),
            "executable": sys.executable, "version": __version__}


@remote_app.command("setup")
def setup_command(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON line (for the desktop app)"),
) -> None:
    """Write $MIRA_HOME/bin/mira (default ~/.mira/bin/mira), a launcher that needs no env activation."""
    _run(setup, as_json=as_json,
         render=lambda p: (f"wrote {p['launcher']} -> {p['executable']} (mira {p['version']})\n"
                           "The desktop app finds the engine on this host through this launcher; "
                           "run `mira remote setup` again after switching environments."))


@remote_app.command("attach")
def attach_command(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON line (for the desktop app)"),
    timeout: float = typer.Option(HEALTH_TIMEOUT_S, "--timeout", help="Seconds to wait for the engine"),
) -> None:
    """Start or attach to this user's engine on 127.0.0.1."""
    _run(lambda: attach(timeout), as_json=as_json,
         render=lambda p: f"engine pid {p['pid']} on 127.0.0.1:{p['port']} "
                          f"({'started' if p['started'] else 'already running'})")


@remote_app.command("status")
def status_command(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON line"),
) -> None:
    """Show whether this user's engine is running."""
    _run(status, as_json=as_json,
         render=lambda p: (f"engine pid {p['pid']} on {p['hostname']} port {p['port']}"
                           if p["running"] else "no engine is running"))


@remote_app.command("stop")
def stop_command(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON line"),
) -> None:
    """Stop this user's standalone or Remote-SSH engine."""
    _run(lambda: {"protocol": PROTOCOL, "ok": True, "message": stop()}, as_json=as_json,
         render=lambda p: p["message"])
