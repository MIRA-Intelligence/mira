"""Allow only one engine per MIRA_HOME.

Two layers, because neither is enough on its own:

- A local OS lock in the runtime dir. It is reliable on local disk and the
  kernel releases it when the process dies, so a crash never wedges restarts.
- A JSON lease record at ``<MIRA_HOME>/engine.lease``. Other tools read it to
  find the running engine (``mira remote attach``), and engines on other hosts
  that share an NFS home use its heartbeat to tell a live holder from a dead
  one. It also covers a runtime dir that was wiped under a live engine (for
  example ``/run/user/<uid>`` removed at logout).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from filelock import FileLock, Timeout
from loguru import logger

from mira_engine.utils.locks import atomic_write_text

LEASE_SCHEMA = 1
LEASE_FILENAME = "engine.lease"
LOCK_FILENAME = "engine.lock"
HEARTBEAT_INTERVAL_S = 30.0
# A record from another host is live while its heartbeat is newer than this.
STALE_AFTER_S = 3 * HEARTBEAT_INTERVAL_S
LEASE_CONFLICT_EXIT_CODE = 3

_ACTIVE: set["EngineLease"] = set()
_ACTIVE_GUARD = threading.Lock()


class LeaseConflictError(RuntimeError):
    """Another live engine already owns this MIRA_HOME."""

    def __init__(self, holder: dict[str, Any] | None, home: Path) -> None:
        self.holder = holder or None
        self.home = home
        if self.holder:
            where = f"host {self.holder.get('hostname', '?')}, pid {self.holder.get('pid', '?')}"
            port = self.holder.get("port")
            if port:
                where += f", port {port}"
            message = f"engine already running for {home} ({where})"
        else:
            message = f"engine already running for {home}"
        super().__init__(message)


def lease_path(home: Path) -> Path:
    return Path(home) / LEASE_FILENAME


def read_lease(home: Path) -> dict[str, Any] | None:
    """Return the lease record, ``{}`` when it is unreadable, or None when absent."""
    path = lease_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _hostname() -> str:
    return socket.gethostname()


def _process_matches(pid: Any, create_time: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if create_time is None:
            return True
        return abs(proc.create_time() - float(create_time)) < 0.01
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # It exists but belongs to someone else; never assume it is gone.
        return True
    except (TypeError, ValueError):
        return False


def holder_alive(record: dict[str, Any] | None, *, now: float | None = None) -> bool:
    """Whether a lease record names an engine that is still running."""
    if not record or record.get("schema") != LEASE_SCHEMA:
        return False
    if record.get("hostname") == _hostname():
        return _process_matches(record.get("pid"), record.get("create_time"))
    heartbeat = record.get("heartbeat_at")
    if not isinstance(heartbeat, (int, float)):
        return False
    return (now if now is not None else time.time()) - heartbeat < STALE_AFTER_S


def _engine_version() -> str:
    from mira_engine import __version__

    return __version__


def _create_exclusive(path: Path, record: dict[str, Any]) -> None:
    # O_EXCL creation is atomic on local disks and on NFSv3+.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record, indent=2) + "\n")


@dataclass(eq=False)
class EngineLease:
    """A held lease. Call :meth:`release` on shutdown (also registered at exit)."""

    home: Path
    record: dict[str, Any]
    _lock: FileLock
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _released: bool = False

    @property
    def instance_id(self) -> str:
        return self.record["instance_id"]

    def _owns_record(self) -> bool:
        current = read_lease(self.home)
        return bool(current) and current.get("instance_id") == self.instance_id

    def update(self, **fields: Any) -> None:
        """Publish new fields (for example the bound port) in the lease record."""
        self.record.update(fields)
        self._write()

    def _write(self) -> None:
        if self._owns_record():
            atomic_write_text(lease_path(self.home), json.dumps(self.record, indent=2) + "\n")

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                if not self._owns_record():
                    logger.warning("Engine lease for {} was taken over by another engine", self.home)
                    return
                self.record["heartbeat_at"] = time.time()
                self._write()
            except OSError as exc:
                logger.warning("Could not refresh engine lease for {}: {}", self.home, exc)

    def start_heartbeat(self) -> None:
        # A thread, not an asyncio task: a busy event loop must not look like a dead engine.
        self._thread = threading.Thread(target=self._heartbeat, name="mira-engine-lease", daemon=True)
        self._thread.start()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._stop.set()
        try:
            if self._owns_record():
                lease_path(self.home).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._lock.release()
        except Exception:  # pragma: no cover - release must never raise during shutdown
            pass
        with _ACTIVE_GUARD:
            _ACTIVE.discard(self)


def acquire_engine_lease(
    *,
    host: str,
    port: int,
    home: Path | None = None,
    launch_mode: str | None = None,
    heartbeat: bool = True,
) -> EngineLease:
    """Take the engine lease for ``home`` or raise :class:`LeaseConflictError`."""
    from mira_engine.config.loader import get_home_dir
    from mira_engine.config.paths import get_runtime_dir

    home = (Path(home) if home is not None else get_home_dir()).expanduser().resolve(strict=False)
    home.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(get_runtime_dir(home) / LOCK_FILENAME), timeout=0, thread_local=False)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise LeaseConflictError(read_lease(home), home) from None

    try:
        record = _claim_record(home, host=host, port=port, launch_mode=launch_mode)
    except BaseException:
        lock.release()
        raise

    lease = EngineLease(home=home, record=record, _lock=lock)
    with _ACTIVE_GUARD:
        _ACTIVE.add(lease)
    if heartbeat:
        lease.start_heartbeat()
    return lease


def _claim_record(home: Path, *, host: str, port: int, launch_mode: str | None) -> dict[str, Any]:
    path = lease_path(home)
    pid = os.getpid()
    now = time.time()
    record = {
        "schema": LEASE_SCHEMA,
        "instance_id": uuid.uuid4().hex,
        "pid": pid,
        "create_time": psutil.Process(pid).create_time(),
        "hostname": _hostname(),
        "host": host,
        "port": port,
        "version": _engine_version(),
        "mira_home": str(home),
        "launch_mode": launch_mode or os.environ.get("MIRA_SERVICE_MODE", "standalone"),
        "started_at": now,
        "heartbeat_at": now,
    }
    for _ in range(2):
        existing = read_lease(home)
        if existing is not None and holder_alive(existing):
            raise LeaseConflictError(existing, home)
        if existing is None:
            try:
                _create_exclusive(path, record)
            except FileExistsError:
                continue  # Someone created it between our read and create; look again.
        else:
            if existing:
                logger.info("Taking over stale engine lease from pid {} on {}",
                            existing.get("pid"), existing.get("hostname"))
            atomic_write_text(path, json.dumps(record, indent=2) + "\n")
        confirmed = read_lease(home)
        if confirmed and confirmed.get("instance_id") == record["instance_id"]:
            return record
        if confirmed and holder_alive(confirmed):
            raise LeaseConflictError(confirmed, home)
    raise LeaseConflictError(read_lease(home), home)


def release_all_leases() -> None:
    """Release every lease held by this process (tests and atexit)."""
    with _ACTIVE_GUARD:
        leases = list(_ACTIVE)
    for lease in leases:
        lease.release()
