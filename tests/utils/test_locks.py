"""Tests for cross-process locking of shared, globally-mutable state."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from mira_engine.utils.locks import (
    LockAcquisitionError,
    interprocess_lock,
    locked_write_text,
)


def _increment_counter(path: str, increments: int) -> None:
    counter = Path(path)
    for _ in range(increments):
        with interprocess_lock(counter):
            current = json.loads(counter.read_text(encoding="utf-8"))["n"]
            counter.write_text(json.dumps({"n": current + 1}), encoding="utf-8")


def _write_with_short_timeout(path: str, result_queue) -> None:
    try:
        locked_write_text(Path(path), "unsafe", timeout=0.05)
    except LockAcquisitionError:
        result_queue.put("blocked")
    else:
        result_queue.put("wrote")


def test_locked_write_text_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    locked_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_locked_write_text_leaves_no_temp_or_lock_residue(tmp_path: Path) -> None:
    target = tmp_path / "MEMORY.md"
    locked_write_text(target, "hello")
    # No stray temp files; the .lock sidecar must be released (not held).
    residue = [p.name for p in tmp_path.iterdir() if p.name.startswith(".") and p.name.endswith(".tmp")]
    assert residue == []


def test_interprocess_lock_yields_true_and_releases(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    with interprocess_lock(target) as acquired:
        assert acquired is True
    # Must be re-acquirable after release.
    with interprocess_lock(target) as acquired_again:
        assert acquired_again is True


def test_lock_serializes_concurrent_read_modify_write(tmp_path: Path) -> None:
    """Independent processes must not lose read-modify-write updates."""
    counter = tmp_path / "counter.json"
    counter.write_text(json.dumps({"n": 0}), encoding="utf-8")

    process_count = 4
    increments_per_process = 25
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(
            target=_increment_counter,
            args=(str(counter), increments_per_process),
        )
        for _ in range(process_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    final = json.loads(counter.read_text(encoding="utf-8"))["n"]
    assert final == process_count * increments_per_process


def test_lock_timeout_never_writes_without_ownership(tmp_path: Path) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("safe", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()

    with interprocess_lock(target):
        process = ctx.Process(
            target=_write_with_short_timeout,
            args=(str(target), result_queue),
        )
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        assert result_queue.get(timeout=2) == "blocked"
        assert target.read_text(encoding="utf-8") == "safe"
