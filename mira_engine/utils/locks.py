"""Cross-process file locking for shared, globally-mutable state.

Multiple ``mira`` instances (concurrent CLIs, or a gateway serving several
projects) share a handful of mutable files under the MIRA home dir -- e.g. the
global ``MEMORY.md``, the activity ``history.jsonl``, ``config.json`` and the
project registry index. Without coordination, concurrent read-modify-write or
append sequences interleave and corrupt each other.

This module provides a small, cross-platform advisory lock (backed by
``filelock``) plus a lock-guarded atomic text writer. Locks are keyed by a
sidecar ``<target>.lock`` file so the target itself is never opened just to
acquire the lock.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from filelock import FileLock, Timeout
from loguru import logger

# Default acquisition timeout. Long enough to ride out a peer's write burst,
# short enough that a crashed holder (stale lock) does not wedge us forever --
# on timeout we log and proceed best-effort rather than raising into user flows.
_DEFAULT_TIMEOUT = 10.0


class LockAcquisitionError(TimeoutError):
    """Raised when shared state cannot be locked before its deadline."""


@contextmanager
def interprocess_lock(target: Path, *, timeout: float = _DEFAULT_TIMEOUT) -> Iterator[bool]:
    """Acquire a cross-process advisory lock guarding ``target``.

    Yields ``True`` after acquiring the lock. A timeout is fail-closed: callers
    must never mutate shared state without owning its lock. The lock uses a
    sidecar ``<target>.lock`` file created alongside the target.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(target) + ".lock")
    try:
        lock.acquire(timeout=timeout)
    except Timeout as exc:
        logger.error(
            "Timed out acquiring lock for {} after {}s; shared state was not modified",
            target,
            timeout,
        )
        raise LockAcquisitionError(
            f"Timed out acquiring lock for {target} after {timeout}s"
        ) from exc
    try:
        yield True
    finally:
        try:
            lock.release()
        except Exception:  # pragma: no cover - release must never raise into callers
            pass


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path``. The caller must provide synchronization."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def locked_write_text(path: Path, content: str, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Atomically write ``content`` to ``path`` under a cross-process lock."""
    path = Path(path)
    with interprocess_lock(path, timeout=timeout):
        atomic_write_text(path, content)


def locked_update_text(
    path: Path,
    update: Callable[[str], str],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """Read, transform, and atomically write text while holding one lock."""
    path = Path(path)
    with interprocess_lock(path, timeout=timeout):
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = update(current)
        atomic_write_text(path, updated)
        return updated
