"""Runtime dir resolution and the single-engine lease (#165)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mira_engine.cli import engine_lease
from mira_engine.cli.engine_lease import (
    LEASE_CONFLICT_EXIT_CODE,
    LeaseConflictError,
    acquire_engine_lease,
    holder_alive,
    lease_path,
    read_lease,
)
from mira_engine.config import paths

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return Path(os.environ["MIRA_HOME"])


# ── runtime dir ──────────────────────────────────────────────────────


def test_runtime_dir_override_is_private_and_namespaced_per_home(tmp_path, monkeypatch):
    base = tmp_path / "run"
    monkeypatch.setenv("MIRA_RUNTIME_DIR", str(base))
    first = paths.get_runtime_dir(tmp_path / "home-a")
    second = paths.get_runtime_dir(tmp_path / "home-b")

    assert first.parent == base and second.parent == base
    assert first != second
    assert first.name == paths.runtime_home_key(tmp_path / "home-a")
    if os.name != "nt":
        assert first.stat().st_mode & 0o777 == 0o700


@POSIX_ONLY
def test_runtime_dir_uses_private_xdg_runtime_dir(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    xdg.mkdir(mode=0o700)
    xdg.chmod(0o700)
    monkeypatch.delenv("MIRA_RUNTIME_DIR")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

    assert paths.get_runtime_dir(tmp_path / "home").parent == xdg / "mira"


@POSIX_ONLY
def test_runtime_dir_ignores_shared_xdg_runtime_dir(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    xdg.chmod(0o755)
    monkeypatch.delenv("MIRA_RUNTIME_DIR")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

    assert paths.get_runtime_dir(tmp_path / "home") == tmp_path / "home" / "runtime"


def test_runtime_dir_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("MIRA_RUNTIME_DIR")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    assert paths.get_runtime_dir(tmp_path / "home") == tmp_path / "home" / "runtime"


def test_windows_never_trusts_xdg_runtime_dir(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setattr(paths, "os", SimpleNamespace(name="nt", environ=os.environ))

    assert paths._is_private_dir(xdg) is False


# ── lease ────────────────────────────────────────────────────────────


def test_lease_publishes_record_and_blocks_a_second_engine(home):
    lease = acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    record = read_lease(home)

    assert record["pid"] == os.getpid()
    assert record["port"] == 18790 and record["host"] == "127.0.0.1"
    assert record["mira_home"] == str(home.resolve())
    assert {"version", "started_at", "heartbeat_at", "hostname", "instance_id"} <= set(record)

    with pytest.raises(LeaseConflictError) as excinfo:
        acquire_engine_lease(host="127.0.0.1", port=18791, heartbeat=False)
    assert excinfo.value.holder["instance_id"] == record["instance_id"]
    assert "pid" in str(excinfo.value)

    lease.release()
    assert read_lease(home) is None
    acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False).release()


def test_lease_update_and_idempotent_release(home):
    lease = acquire_engine_lease(host="127.0.0.1", port=0, heartbeat=False)
    lease.update(port=40123)
    assert read_lease(home)["port"] == 40123
    lease.release()
    lease.release()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_record(home: Path, **fields) -> None:
    home.mkdir(parents=True, exist_ok=True)
    record = {"schema": 1, "instance_id": "old", "hostname": engine_lease._hostname(),
              "pid": _dead_pid(), "create_time": 1.0, "port": 1, "heartbeat_at": time.time()}
    record.update(fields)
    lease_path(home).write_text(json.dumps(record), encoding="utf-8")


def test_stale_record_from_dead_process_is_taken_over(home):
    _write_record(home)
    lease = acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    assert read_lease(home)["instance_id"] == lease.instance_id
    lease.release()


def test_corrupt_record_is_taken_over(home):
    home.mkdir(parents=True, exist_ok=True)
    lease_path(home).write_text("{not json", encoding="utf-8")
    lease = acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    assert read_lease(home)["instance_id"] == lease.instance_id
    lease.release()


def test_live_record_blocks_even_when_runtime_lock_was_wiped(home, tmp_path, monkeypatch):
    # e.g. /run/user/<uid> removed at logout while the engine kept running.
    lease = acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    monkeypatch.setenv("MIRA_RUNTIME_DIR", str(tmp_path / "fresh-run"))

    with pytest.raises(LeaseConflictError):
        acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    assert read_lease(home)["instance_id"] == lease.instance_id
    lease.release()


def test_other_host_with_fresh_heartbeat_blocks(home):
    _write_record(home, hostname="other-node", pid=4242, heartbeat_at=time.time())
    with pytest.raises(LeaseConflictError) as excinfo:
        acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    assert excinfo.value.holder["hostname"] == "other-node"


def test_other_host_with_stale_heartbeat_is_taken_over(home):
    _write_record(home, hostname="other-node", pid=4242,
                  heartbeat_at=time.time() - engine_lease.STALE_AFTER_S - 5)
    lease = acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    assert read_lease(home)["hostname"] == engine_lease._hostname()
    lease.release()


def test_holder_alive_rejects_unknown_schema():
    assert holder_alive({"schema": 99, "hostname": engine_lease._hostname(), "pid": os.getpid()}) is False
    assert holder_alive({}) is False
    assert holder_alive(None) is False


def test_heartbeat_refreshes_and_stops_after_takeover(home, monkeypatch):
    monkeypatch.setattr(engine_lease, "HEARTBEAT_INTERVAL_S", 0.05)
    lease = acquire_engine_lease(host="127.0.0.1", port=18790)
    first = read_lease(home)["heartbeat_at"]
    deadline = time.time() + 5
    while read_lease(home)["heartbeat_at"] == first and time.time() < deadline:
        time.sleep(0.02)
    assert read_lease(home)["heartbeat_at"] > first

    # Another host took the lease over: stop refreshing and never delete its record.
    foreign = {**read_lease(home), "instance_id": "foreign", "hostname": "other-node"}
    lease_path(home).write_text(json.dumps(foreign), encoding="utf-8")
    lease._thread.join(timeout=5)
    assert not lease._thread.is_alive()
    lease.release()
    assert read_lease(home)["instance_id"] == "foreign"


_CONTENDER = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from mira_engine.cli.engine_lease import LeaseConflictError, acquire_engine_lease

    go = Path(sys.argv[1])
    while not go.exists():
        time.sleep(0.005)
    try:
        lease = acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    except LeaseConflictError:
        print("lost", flush=True)
        sys.exit(0)
    print("won", flush=True)
    time.sleep(1.5)
    lease.release()
    """
)


def test_concurrent_starts_elect_exactly_one_engine(tmp_path):
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    procs = [
        subprocess.Popen([sys.executable, "-c", _CONTENDER, str(go)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    time.sleep(0.5)
    go.touch()
    results = [proc.communicate(timeout=60) for proc in procs]

    outcomes = sorted(out.strip() for out, _ in results)
    assert outcomes == ["lost", "lost", "lost", "won"], results


# ── gateway integration ─────────────────────────────────────────────


def test_gateway_refuses_to_start_while_another_engine_holds_the_lease(home):
    from mira_engine.cli.commands import app

    lease = acquire_engine_lease(host="127.0.0.1", port=18790, heartbeat=False)
    result = CliRunner().invoke(app, ["gateway", "--port", "18795"])

    assert result.exit_code == LEASE_CONFLICT_EXIT_CODE
    assert "engine already running" in result.output
    assert read_lease(home)["instance_id"] == lease.instance_id
    lease.release()
