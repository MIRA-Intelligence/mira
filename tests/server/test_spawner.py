"""Spawner output parsing and error mapping, launcher commands, listener uid checks."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from mira_server.auth.identity import Identity
from mira_server.spawner.base import (
    CONFLICT,
    DENIED,
    HEALTH_TIMEOUT,
    INTERNAL,
    PROTOCOL_MISMATCH,
    START_FAILED,
    TIMEOUT,
    LauncherSpawner,
    SpawnerError,
    handle_from_payload,
    parse_remote_output,
)
from mira_server.spawner.local import LocalSpawner
from mira_server.spawner.netcheck import (
    check_listener,
    check_proc_listener,
    check_psutil_listener,
    parse_proc_net_tcp,
)
from mira_server.spawner.sudo import SudoSpawner

ALICE = Identity("alice", 1001, "/home/alice", "Alice", ("user",))
PROC = Path(__file__).parent / "fixtures" / "proc"


def _line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


# ── parsing `mira remote ... --json` ─────────────────────────────────


def test_parse_attach_success() -> None:
    payload = parse_remote_output(
        b"some stray output\n" + _line({"protocol": 1, "ok": True, "port": 18790, "token": "t", "version": "1.0",
                                         "pid": 42, "mira_home": "/home/alice/.mira", "started": True}),
        b"", 0)
    handle = handle_from_payload(payload)
    assert (handle.port, handle.token, handle.pid, handle.version, handle.started) == (18790, "t", 42, "1.0", True)
    assert handle.mira_home == "/home/alice/.mira"


@pytest.mark.parametrize("code,message,expected", [
    ("lease_conflict", "an engine for /home/alice/.mira is running on host b", CONFLICT),
    ("config_invalid", "the running engine rejected the local gateway token", CONFLICT),
    ("config_invalid", "the running engine listens on 10.0.0.1, which ...", CONFLICT),
    ("config_invalid", "cannot load the Mira config: bad json", START_FAILED),
    ("start_failed", "the engine exited with code 1", START_FAILED),
    ("health_timeout", "did not become healthy", HEALTH_TIMEOUT),
    ("internal", "boom", INTERNAL),
    ("something_new", "?", INTERNAL),
])
def test_error_mapping(code, message, expected) -> None:
    out = _line({"protocol": 1, "ok": False, "error": {"code": code, "message": message}})
    with pytest.raises(SpawnerError) as info:
        parse_remote_output(out, b"", 1)
    assert info.value.code == expected
    assert info.value.message == message
    assert info.value.conflict == (expected == CONFLICT)


def test_protocol_mismatch() -> None:
    with pytest.raises(SpawnerError) as info:
        parse_remote_output(_line({"protocol": 2, "ok": True}), b"", 0)
    assert info.value.code == PROTOCOL_MISMATCH


@pytest.mark.parametrize("stderr", [
    b"sudo: a password is required\n",
    b"Sorry, user mira-server is not allowed to execute '/usr/local/libexec/mira/engine-launch start' "
    b"as alice on host.\n",
])
def test_sudo_refusal_is_denied(stderr) -> None:
    with pytest.raises(SpawnerError) as info:
        parse_remote_output(b"", stderr, 1)
    assert info.value.code == DENIED


def test_garbage_output_is_start_failed() -> None:
    with pytest.raises(SpawnerError) as info:
        parse_remote_output(b"not json\n", b"Traceback ...\nImportError: no module named mira_engine\n", 1)
    assert info.value.code == START_FAILED
    assert "ImportError" in info.value.message


@pytest.mark.parametrize("payload", [
    {"protocol": 1, "ok": True, "token": "t"},
    {"protocol": 1, "ok": True, "port": 0, "token": "t"},
    {"protocol": 1, "ok": True, "port": 18790},
])
def test_handle_requires_port_and_token(payload) -> None:
    with pytest.raises(SpawnerError):
        handle_from_payload(payload)


# ── commands ─────────────────────────────────────────────────────────


def test_sudo_command() -> None:
    spawner = SudoSpawner(sudo=Path("/usr/bin/sudo"), launcher=Path("/usr/local/libexec/mira/engine-launch"),
                          start_timeout=5, stop_timeout=5)
    argv, env = spawner.command(ALICE, "start")
    assert argv == ["/usr/bin/sudo", "-n", "-u", "alice", "--", "/usr/local/libexec/mira/engine-launch", "start"]
    assert "MIRA_GATEWAY_TOKEN" not in env and set(env) == {"PATH", "LANG"}
    assert spawner.expected_uid(ALICE) == 1001
    with pytest.raises(SpawnerError):
        spawner.command(ALICE, "rm -rf")
    with pytest.raises(SpawnerError):
        spawner.command(Identity("-x", 1, "/", "x", ("user",)), "start")


def test_local_command(monkeypatch) -> None:
    monkeypatch.setenv("MIRA_GATEWAY_TOKEN", "leak")
    spawner = LocalSpawner(engine_python="/py", mira_home_template="/tmp/dev/{user}", start_timeout=5,
                           stop_timeout=5)
    argv, env = spawner.command(ALICE, "start")
    assert argv == ["/py", "-m", "mira_engine", "remote", "attach", "--json", "--timeout", "60"]
    assert env["MIRA_HOME"] == "/tmp/dev/alice"
    assert "MIRA_GATEWAY_TOKEN" not in env
    assert spawner.command(ALICE, "status")[0][-2:] == ["status", "--json"]
    assert spawner.command(ALICE, "stop")[0][-2:] == ["stop", "--json"]
    assert spawner.expected_uid(ALICE) == os.getuid()


class ScriptSpawner(LauncherSpawner):
    """Runs a small Python script in place of the launcher."""

    def __init__(self, script: str, **kwargs) -> None:
        super().__init__(start_timeout=kwargs.get("timeout", 10), stop_timeout=kwargs.get("timeout", 10))
        self.script = script

    def command(self, identity, action):
        return [sys.executable, "-c", self.script, action], {"PATH": os.environ.get("PATH", "")}

    def expected_uid(self, identity):
        return os.getuid()


async def test_launcher_spawner_runs_and_parses() -> None:
    script = (
        "import json, sys\n"
        "a = sys.argv[1]\n"
        "print('noise', file=sys.stderr)\n"
        "if a == 'start': print(json.dumps({'protocol': 1, 'ok': True, 'port': 4242, 'token': 'tok', 'pid': 7, 'started': True}))\n"
        "elif a == 'status': print(json.dumps({'protocol': 1, 'ok': True, 'running': False}))\n"
        "else: print(json.dumps({'protocol': 1, 'ok': True, 'message': 'stopped'}))\n"
    )
    spawner = ScriptSpawner(script)
    handle = await spawner.start(ALICE)
    assert (handle.port, handle.token, handle.pid, handle.started) == (4242, "tok", 7, True)
    assert await spawner.status(ALICE) is None
    await spawner.stop(ALICE)


async def test_status_on_other_host_is_conflict() -> None:
    script = ("import json; print(json.dumps({'protocol': 1, 'ok': True, 'running': True, 'port': 1, "
              "'hostname': 'other'}))")
    with pytest.raises(SpawnerError) as info:
        await ScriptSpawner(script).status(ALICE)
    assert info.value.code == CONFLICT


async def test_launcher_timeout() -> None:
    spawner = ScriptSpawner("import time; time.sleep(30)", timeout=0.5)
    with pytest.raises(SpawnerError) as info:
        await spawner.start(ALICE)
    assert info.value.code == TIMEOUT


async def test_missing_launcher_binary() -> None:
    spawner = SudoSpawner(sudo=Path("/nonexistent/sudo"), launcher=Path("/x"), start_timeout=1, stop_timeout=1)
    with pytest.raises(SpawnerError) as info:
        await spawner.start(ALICE)
    assert info.value.code == INTERNAL


# ── listener uid verification ────────────────────────────────────────


def test_parse_proc_net_tcp_keeps_only_listeners() -> None:
    listeners = parse_proc_net_tcp((PROC / "net" / "tcp").read_text())
    assert [(str(item.address), item.port, item.uid) for item in listeners] == [
        ("127.0.0.1", 18806, 1001), ("0.0.0.0", 22, 0), ("10.0.0.10", 18810, 1006)]
    v6 = parse_proc_net_tcp((PROC / "net" / "tcp6").read_text())
    assert [(str(item.address), item.port, item.uid) for item in v6] == [
        ("::1", 18807, 1002), ("::", 8081, 0), ("::ffff:127.0.0.1", 18808, 1003),
        ("fe80::fea6:57ff:fe21:1b6b", 18809, 1004)]


@pytest.mark.parametrize("port,uid,ok", [
    (18806, 1001, True),    # 127.0.0.1, right owner
    (18806, 1002, False),   # 127.0.0.1, someone else's socket
    (18807, 1002, True),    # ::1
    (18808, 1003, True),    # ::ffff:127.0.0.1
    (8081, 0, True),        # wildcard :: reaches loopback
    (18809, 1004, False),   # link-local only: not reachable through 127.0.0.1
    (18810, 1006, False),   # a non-loopback address only
    (9999, 1001, False),    # nothing listening
])
def test_check_proc_listener(port, uid, ok) -> None:
    result = check_proc_listener(port, uid, proc_root=PROC)
    assert result.ok is ok, result.reason


def test_any_foreign_listener_on_the_port_fails(tmp_path) -> None:
    net = tmp_path / "net"
    net.mkdir()
    (net / "tcp").write_text((PROC / "net" / "tcp").read_text())
    header = (PROC / "net" / "tcp6").read_text().splitlines()[0]
    # Another user also listens on [::]:18806 (a dual-stack squat).
    (net / "tcp6").write_text(header + "\n   0: 00000000000000000000000000000000:4976 "
                              "00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 "
                              "00000000  1005        0 9 1 0000000000000000 100 0 0 10 0\n")
    result = check_proc_listener(18806, 1001, proc_root=tmp_path)
    assert not result.ok and result.uids == (1001, 1005)


@pytest.fixture
def listening_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    yield sock.getsockname()[1]
    sock.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/net/tcp is Linux only")
def test_live_proc_check(listening_port) -> None:
    assert check_listener(listening_port, os.getuid()).ok
    assert not check_listener(listening_port, os.getuid() + 1).ok


def test_psutil_check(listening_port) -> None:
    pid = os.getpid()
    result = check_psutil_listener(listening_port, os.getuid(), pid)
    if not result.ok and "cannot inspect" in result.reason:
        pytest.skip(result.reason)
    assert result.ok, result.reason
    assert not check_psutil_listener(listening_port, os.getuid() + 1, pid).ok
    assert not check_psutil_listener(1, os.getuid(), pid).ok
    assert not check_psutil_listener(listening_port, os.getuid(), None).ok
