"""`mira remote attach/status/stop` for Remote-SSH (#166)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mira_engine.cli import engine_lease, remote
from mira_engine.cli.commands import app
from mira_engine.cli.engine_lease import acquire_engine_lease, lease_path, read_lease
from mira_engine.cli.port_guard import PORT_IN_USE_EXIT_CODE

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stands in for `mira gateway`: takes the real lease, then serves /health and /api/config.
_FAKE_GATEWAY = textwrap.dedent(
    """
    import sys, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from mira_engine.cli.engine_lease import acquire_engine_lease

    port, mode = int(sys.argv[1]), sys.argv[2]
    if mode == "crash":
        print("boom: provider misconfigured", flush=True)
        sys.exit(1)
    if mode == "port-in-use":
        sys.exit(4)  # PORT_IN_USE_EXIT_CODE: the port was taken by a process the gateway must not stop
    lease = acquire_engine_lease(host="127.0.0.1", port=port)
    if mode == "hang":
        time.sleep(60)
        sys.exit(0)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 401 if mode == "reject-token" and self.path.startswith("/api/") else 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    """
)


@pytest.fixture
def spawned(monkeypatch):
    """Replace the real gateway launch with the fake one; kill children afterwards."""
    children: list[subprocess.Popen] = []
    state = {"mode": "serve"}
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}

    def fake_spawn(port: int) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", _FAKE_GATEWAY, str(port), state["mode"]],
                                env={**env, **{k: os.environ[k] for k in ("MIRA_HOME", "MIRA_RUNTIME_DIR")}},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(proc)
        return proc

    monkeypatch.setattr(remote, "_spawn_gateway", fake_spawn)
    yield state, children
    for proc in children:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(autouse=True)
def default_port(monkeypatch):
    # Never collide with a developer's real engine on 18790.
    port = _free_port()
    monkeypatch.setattr(remote, "DEFAULT_PORT", port)
    return port


def _invoke(*args: str):
    result = CliRunner().invoke(app, ["remote", *args])
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"stdout must be exactly one JSON line: {result.stdout!r}"
    return result.exit_code, json.loads(lines[0])


def _write_record(**fields) -> None:
    home = Path(os.environ["MIRA_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    record = {"schema": 1, "instance_id": "x", "hostname": engine_lease._hostname(), "pid": os.getpid(),
              "create_time": None, "host": "127.0.0.1", "port": 1, "version": "0", "heartbeat_at": time.time()}
    record.update(fields)
    lease_path(home).write_text(json.dumps(record), encoding="utf-8")


# ── attach ───────────────────────────────────────────────────────────


def test_attach_starts_engine_then_reuses_it(spawned, default_port):
    code, first = _invoke("attach", "--json")
    assert code == 0
    assert first["ok"] is True and first["protocol"] == 1 and first["started"] is True
    assert first["port"] == default_port
    assert first["token"] and first["version"] and first["pid"]
    assert first["mira_home"] == str(Path(os.environ["MIRA_HOME"]).resolve())

    code, second = _invoke("attach", "--json")
    assert code == 0
    assert second["started"] is False
    assert (second["pid"], second["port"], second["token"]) == (first["pid"], first["port"], first["token"])
    assert len(spawned[1]) == 1


def test_attach_uses_a_random_port_when_the_default_is_taken(spawned, default_port):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", default_port))
        busy.listen()
        code, payload = _invoke("attach", "--json")
    assert code == 0
    assert payload["port"] != default_port


def test_attach_retries_when_the_gateway_finds_its_port_taken(spawned, monkeypatch):
    state, children = spawned
    first_spawn = remote._spawn_gateway

    def spawn(port: int):
        proc = first_spawn(port)
        state["mode"] = "serve"
        return proc

    state["mode"] = "port-in-use"
    monkeypatch.setattr(remote, "_spawn_gateway", spawn)
    code, payload = _invoke("attach", "--json")
    assert code == 0 and payload["started"] is True
    assert len(children) == 2
    assert children[0].returncode == PORT_IN_USE_EXIT_CODE


def test_attach_replaces_a_stale_lease(spawned):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write_record(pid=dead.pid, create_time=1.0)

    code, payload = _invoke("attach", "--json")
    assert code == 0 and payload["started"] is True
    assert read_lease(Path(os.environ["MIRA_HOME"]))["pid"] == payload["pid"]


def test_stdout_stays_clean_when_work_prints(monkeypatch):
    def noisy():
        print("chatty import output")
        return {"protocol": 1, "ok": True, "port": 1}

    monkeypatch.setattr(remote, "attach", lambda _timeout: noisy())
    code, payload = _invoke("attach", "--json")
    assert code == 0 and payload["port"] == 1


@pytest.mark.parametrize(
    ("mode", "code"),
    [("crash", "start_failed"), ("hang", "health_timeout"), ("reject-token", "config_invalid")],
)
def test_attach_failure_codes(spawned, mode, code):
    spawned[0]["mode"] = mode
    exit_code, payload = _invoke("attach", "--json", "--timeout", "3")
    assert exit_code == 1
    assert payload["ok"] is False and payload["protocol"] == 1
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


def test_attach_reports_engine_on_another_host_as_lease_conflict(spawned):
    _write_record(hostname="other-node", pid=4242)
    exit_code, payload = _invoke("attach", "--json")
    assert exit_code == 1
    assert payload["error"]["code"] == "lease_conflict"
    assert "other-node" in payload["error"]["message"]
    assert spawned[1] == []


def test_attach_refuses_an_engine_bound_to_a_public_address(spawned):
    lease = acquire_engine_lease(host="192.0.2.10", port=_free_port(), heartbeat=False)
    exit_code, payload = _invoke("attach", "--json")
    lease.release()
    assert exit_code == 1 and payload["error"]["code"] == "config_invalid"


def test_attach_reports_invalid_config(monkeypatch):
    def broken(*_args, **_kwargs):
        raise ValueError("bad json")

    monkeypatch.setattr("mira_engine.config.loader.load_config", broken)
    exit_code, payload = _invoke("attach", "--json")
    assert exit_code == 1 and payload["error"]["code"] == "config_invalid"


def test_unexpected_errors_still_produce_one_json_line(monkeypatch):
    def explode(_timeout):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(remote, "attach", explode)
    exit_code, payload = _invoke("attach", "--json")
    assert exit_code == 1
    assert payload["error"] == {"code": "internal", "message": "RuntimeError: kaboom"}


# ── status / stop ────────────────────────────────────────────────────


def test_status_without_engine():
    code, payload = _invoke("status", "--json")
    assert code == 0
    assert payload == {"protocol": 1, "ok": True, "running": False,
                       "mira_home": str(Path(os.environ["MIRA_HOME"]).resolve())}


def test_status_reports_remote_host_without_token():
    _write_record(hostname="other-node", pid=4242, port=18790)
    code, payload = _invoke("status", "--json")
    assert code == 0 and payload["running"] is True
    assert payload["hostname"] == "other-node"
    assert "token" not in payload


def test_stop_stops_the_engine_and_clears_its_lease(spawned, monkeypatch):
    monkeypatch.setattr("mira_engine.cli.gateway_lifecycle.is_gateway_command", lambda _cmd: True)
    _invoke("attach", "--json")

    code, payload = _invoke("stop", "--json")
    assert code == 0 and payload["ok"] is True
    assert spawned[1][0].poll() is not None
    assert read_lease(Path(os.environ["MIRA_HOME"])) is None

    code, payload = _invoke("stop", "--json")
    assert code == 0 and payload["message"] == "no engine is running"


def test_stop_refuses_service_managed_engines():
    _write_record(launch_mode="launchd")
    code, payload = _invoke("stop", "--json")
    assert code == 1 and payload["error"]["code"] == "lease_conflict"


def test_stop_refuses_processes_that_are_not_gateways(spawned):
    _invoke("attach", "--json")
    code, payload = _invoke("stop", "--json")
    assert code == 1 and "not a Mira gateway" in payload["error"]["message"]
    assert spawned[1][0].poll() is None


# ── gateway guard ────────────────────────────────────────────────────


def test_remote_ssh_gateway_refuses_non_loopback_bind(monkeypatch):
    monkeypatch.setenv(remote.LAUNCH_MODE_ENV, remote.REMOTE_LAUNCH_MODE)
    result = CliRunner().invoke(app, ["gateway", "--host", "0.0.0.0", "--port", str(_free_port())])
    assert result.exit_code == 2
    assert "loopback" in result.stderr
    assert read_lease(Path(os.environ["MIRA_HOME"])) is None
