"""Background subprocess registry and the ``bg`` companion tool.

The vanilla :class:`~mira_engine.agent.tools.shell.ExecTool` runs a command in
the foreground and waits for it, capped by ``_MAX_TIMEOUT`` (10 minutes). That
is fine for almost everything an agent does, but it makes long-running
scientific work — neural-net training, big data preprocessing, large fits —
impossible to run in a single tool call.

This module adds the *background job* primitive:

* The agent calls ``exec(command=..., background=true)``. The tool spawns a real
  shell subprocess, streams ``stdout`` / ``stderr`` to ``stdout.log`` /
  ``stderr.log`` inside ``<workspace>/.mira/jobs/<job_id>/``, registers the
  job in a process-wide :class:`BackgroundJobRegistry`, and returns
  immediately with the ``job_id`` and ``pid``.
* The agent then uses the :class:`BgTool` (exposed as ``bg``) to ``status``,
  ``tail``, ``wait``, or ``kill`` the job across as many agent loop
  iterations as it needs.

The registry is owned by the loop, so when the loop shuts down all live jobs
are best-effort terminated. We deliberately do **not** persist jobs across
engine restarts — durable tracking is a separate, larger feature.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.agent.tools.base import Tool

_IS_WINDOWS = sys.platform == "win32"

_MAX_WAIT_TIMEOUT = 600
"""Largest single ``bg.wait`` blocking window. The agent can simply call again."""

_DEFAULT_TAIL_LINES = 40
_MAX_TAIL_LINES = 1000

_KILL_GRACE_SECONDS = 5.0
"""SIGTERM-then-SIGKILL grace window for :meth:`BackgroundJob.kill`."""

_COMMAND_PREVIEW_CHARS = 200


def _terminate_windows_tree(pid: int, force: bool) -> None:
    """Terminate a Windows process and all descendants."""
    try:
        import psutil

        parent = psutil.Process(pid)
        processes = parent.children(recursive=True)
        processes.append(parent)
        for process in reversed(processes):
            try:
                process.kill() if force else process.terminate()
            except psutil.Error:
                pass
    except Exception:
        flags = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            flags.append("/F")
        subprocess.run(flags, capture_output=True, check=False)


def _utc_iso(ts: float) -> str:
    """Format ``ts`` (seconds since epoch) as a compact ISO-8601 UTC string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _tail_file(path: Path, lines: int) -> str:
    """Return the last ``lines`` lines of ``path`` as a single string.

    Returns an empty string if the file does not exist yet (e.g. the process
    hasn't written any output).
    """
    if not path.exists():
        return ""
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
            except OSError:
                return path.read_text(errors="replace")
            block_size = 8192
            data = b""
            blocks = 0
            while size > 0 and data.count(b"\n") <= lines:
                read_size = min(block_size, size)
                size -= read_size
                fh.seek(size)
                data = fh.read(read_size) + data
                blocks += 1
                if blocks > 256:
                    break
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError:
        return ""


@dataclass
class BackgroundJob:
    """In-memory record for a single background subprocess.

    ``process`` is the live :class:`asyncio.subprocess.Process`; the registry
    keeps it alive so the OS does not reap the child until we explicitly
    ``await process.wait()``. Once the process exits we record ``exit_code``
    and ``exited_at`` but keep the job in the registry until the loop shuts
    down so the agent can still inspect logs.
    """

    job_id: str
    command: str
    pid: int
    cwd: str
    log_dir: Path
    stdout_path: Path
    stderr_path: Path
    process: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.time)
    exited_at: float | None = None
    exit_code: int | None = None
    description: str | None = None
    owner_session_id: str | None = None
    owner_turn_id: str | None = None

    @property
    def running(self) -> bool:
        """``True`` while the subprocess is alive.

        We trust :attr:`Process.returncode` over polling the OS — asyncio sets
        it as soon as ``wait`` resolves, and we keep a background reaper task
        running that surfaces exits promptly even when nobody calls ``wait``.
        """
        return self.process.returncode is None

    def command_preview(self, limit: int = _COMMAND_PREVIEW_CHARS) -> str:
        """Return a single-line, length-capped command preview for displays."""
        flat = " ".join(self.command.split())
        if len(flat) <= limit:
            return flat
        return flat[: limit - 1] + "…"

    def status_label(self) -> str:
        if self.running:
            return "running"
        if self.exit_code == 0:
            return "exited"
        if self.exit_code is None:
            return "unknown"
        return f"failed({self.exit_code})"

    def to_summary(self) -> dict[str, Any]:
        """Lightweight dict representation used by ``bg list`` / ``bg status``."""
        summary: dict[str, Any] = {
            "job_id": self.job_id,
            "pid": self.pid,
            "status": self.status_label(),
            "command": self.command_preview(),
            "started_at": _utc_iso(self.started_at),
            "log_dir": str(self.log_dir),
        }
        if self.description:
            summary["description"] = self.description
        if self.owner_session_id:
            summary["owner_session_id"] = self.owner_session_id
        if self.owner_turn_id:
            summary["owner_turn_id"] = self.owner_turn_id
        if self.exited_at is not None:
            summary["exited_at"] = _utc_iso(self.exited_at)
        if self.exit_code is not None:
            summary["exit_code"] = self.exit_code
        if self.running:
            summary["elapsed_s"] = round(time.time() - self.started_at, 1)
        elif self.exited_at is not None:
            summary["elapsed_s"] = round(self.exited_at - self.started_at, 1)
        return summary

    async def kill(self) -> None:
        """Send SIGTERM, then SIGKILL after a grace window if still alive.

        On Windows we go straight to ``terminate`` because we don't have
        ``SIGTERM`` semantics there.
        """
        if not self.running:
            return
        try:
            if _IS_WINDOWS:
                await asyncio.to_thread(_terminate_windows_tree, self.pid, False)
            else:
                os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=_KILL_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if _IS_WINDOWS:
                await asyncio.to_thread(_terminate_windows_tree, self.pid, True)
            else:
                os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "Background job {} (pid={}) did not exit after SIGKILL",
                self.job_id,
                self.pid,
            )


class BackgroundJobRegistry:
    """Process-wide registry of live :class:`BackgroundJob` instances.

    Shared between :class:`~mira_engine.agent.tools.shell.ExecTool` (which
    inserts new jobs) and :class:`BgTool` (which queries / kills them). One
    instance per :class:`BaseAgentLoop`.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, BackgroundJob] = {}
        self._reapers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def __contains__(self, job_id: str) -> bool:
        return job_id in self._jobs

    def __len__(self) -> int:
        return len(self._jobs)

    def list(self) -> list[BackgroundJob]:
        """Return jobs sorted by start time (newest first)."""
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def get(self, job_id: str) -> BackgroundJob | None:
        return self._jobs.get(job_id)

    @staticmethod
    def new_job_id() -> str:
        return f"bg-{uuid.uuid4().hex[:8]}"

    async def register(self, job: BackgroundJob) -> None:
        """Add ``job`` and start a background reaper that records exit metadata."""
        if self._closed:
            raise RuntimeError("BackgroundJobRegistry is closed")
        async with self._lock:
            self._jobs[job.job_id] = job
            self._reapers[job.job_id] = asyncio.create_task(self._reap(job))

    async def _reap(self, job: BackgroundJob) -> None:
        """Wait for ``job`` to exit and stamp its exit metadata.

        Failures are swallowed because this runs as a fire-and-forget task —
        the alternative is leaking exceptions into the asyncio loop's error
        handler, which would surface as scary logs for an expected event.
        """
        try:
            rc = await job.process.wait()
            job.exit_code = rc
            job.exited_at = time.time()
            logger.info(
                "Background job {} exited with code {} after {:.1f}s",
                job.job_id,
                rc,
                job.exited_at - job.started_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reaper for background job {} failed", job.job_id)

    async def shutdown(self) -> None:
        """Kill all live jobs and cancel reapers. Idempotent."""
        if self._closed:
            return
        self._closed = True
        jobs = list(self._jobs.values())
        for job in jobs:
            try:
                await job.kill()
            except Exception:
                logger.exception(
                    "Failed to terminate background job {} during shutdown",
                    job.job_id,
                )
        for reaper in self._reapers.values():
            reaper.cancel()
        for reaper in self._reapers.values():
            try:
                await reaper
            except (asyncio.CancelledError, Exception):
                pass
        self._reapers.clear()

    async def kill_by_session(self, session_id: str) -> int:
        """Terminate every live job owned by ``session_id``."""
        jobs = [
            job for job in self._jobs.values()
            if job.owner_session_id == session_id and job.running
        ]
        for job in jobs:
            try:
                await job.kill()
            except Exception:
                logger.exception("Failed to terminate background job {}", job.job_id)
        return len(jobs)


async def spawn_background_job(
    *,
    registry: BackgroundJobRegistry,
    command: str,
    cwd: str,
    env: dict[str, str],
    description: str | None = None,
    job_dir_root: Path | None = None,
    owner_session_id: str | None = None,
    owner_turn_id: str | None = None,
) -> BackgroundJob:
    """Spawn ``command`` as a detached shell subprocess and register it.

    The caller is expected to have already applied any sandboxing / PATH
    munging it cares about — this function just spawns and bookkeeps. We use
    ``bash -l -c`` on POSIX (matching :class:`ExecTool`) and ``cmd /c`` on
    Windows so the shell semantics line up with foreground ``exec``.
    """
    job_id = registry.new_job_id()
    base = job_dir_root or (Path(cwd) / ".mira" / "jobs")
    log_dir = base / job_id
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"

    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        if _IS_WINDOWS:
            comspec = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
            process = await asyncio.create_subprocess_exec(
                comspec,
                "/c",
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=cwd,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-l",
                "-c",
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
    finally:
        # asyncio dup'd the handles into the child; we can drop ours so the
        # OS can free the descriptors as soon as the child exits. The child
        # keeps writing through its own copies.
        try:
            stdout_handle.close()
        except Exception:
            pass
        try:
            stderr_handle.close()
        except Exception:
            pass

    job = BackgroundJob(
        job_id=job_id,
        command=command,
        pid=process.pid,
        cwd=cwd,
        log_dir=log_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        process=process,
        description=description,
        owner_session_id=owner_session_id,
        owner_turn_id=owner_turn_id,
    )
    await registry.register(job)
    logger.info(
        "Started background job {} (pid={}) in {}: {}",
        job.job_id,
        job.pid,
        cwd,
        job.command_preview(),
    )
    return job


_ACTIONS = ("list", "status", "wait", "kill", "tail")


class BgTool(Tool):
    """Inspect and control background jobs spawned via ``exec(background=true)``.

    The action enum keeps the LLM's tool surface to a single name. Each action
    only consults the registry — it never spawns new processes.
    """

    def __init__(self, registry: BackgroundJobRegistry) -> None:
        self.registry = registry

    @property
    def name(self) -> str:
        return "bg"

    @property
    def description(self) -> str:
        return (
            "Inspect, wait on, or terminate background jobs started via "
            "`exec(background=true)`. Use `list` to see active jobs, "
            "`status` for one job's metadata, `tail` to read recent log "
            "output, `wait` to block up to `timeout` seconds for completion, "
            "and `kill` to terminate a runaway job."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": (
                        "What to do: list (all jobs), status / tail / wait / "
                        "kill (target one job by job_id)."
                    ),
                },
                "job_id": {
                    "type": "string",
                    "description": "Target job id, e.g. bg-1a2b3c4d. Required for everything except `list`.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_WAIT_TIMEOUT,
                    "description": (
                        f"Seconds to block on `wait` (1-{_MAX_WAIT_TIMEOUT}). "
                        "Defaults to 30. Returns 'still running' if exceeded."
                    ),
                },
                "tail_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TAIL_LINES,
                    "description": (
                        f"How many trailing log lines to include (1-{_MAX_TAIL_LINES}). "
                        f"Defaults to {_DEFAULT_TAIL_LINES}."
                    ),
                },
            },
            "required": ["action"],
        }

    @property
    def read_only(self) -> bool:
        # `kill` mutates state; rather than vary per-call we conservatively
        # mark the whole tool as side-effecting so the loop scheduler treats
        # it like other write tools.
        return False

    async def execute(self, action: str, **kwargs: Any) -> str:
        action = (action or "").lower().strip()
        if action not in _ACTIONS:
            return f"Error: unknown action '{action}'. Choose one of: {', '.join(_ACTIONS)}."

        if action == "list":
            return self._render_list()

        job_id = (kwargs.get("job_id") or "").strip()
        if not job_id:
            return f"Error: action '{action}' requires job_id."

        job = self.registry.get(job_id)
        if job is None:
            return f"Error: no background job with id '{job_id}'. Use action='list' to see active jobs."

        if action == "status":
            return self._render_status(job)
        if action == "tail":
            tail_lines = self._coerce_int(kwargs.get("tail_lines"), _DEFAULT_TAIL_LINES, 1, _MAX_TAIL_LINES)
            return self._render_tail(job, tail_lines)
        if action == "kill":
            return await self._do_kill(job)
        if action == "wait":
            timeout = self._coerce_int(kwargs.get("timeout"), 30, 1, _MAX_WAIT_TIMEOUT)
            tail_lines = self._coerce_int(kwargs.get("tail_lines"), _DEFAULT_TAIL_LINES, 1, _MAX_TAIL_LINES)
            return await self._do_wait(job, timeout, tail_lines)

        return f"Error: action '{action}' is not implemented."

    @staticmethod
    def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
        try:
            n = int(value) if value is not None else default
        except (TypeError, ValueError):
            n = default
        return max(lo, min(hi, n))

    def _render_list(self) -> str:
        jobs = self.registry.list()
        if not jobs:
            return "No background jobs."
        lines = [f"Background jobs ({len(jobs)}):"]
        for job in jobs:
            summary = job.to_summary()
            extra = []
            if "elapsed_s" in summary:
                extra.append(f"{summary['elapsed_s']}s")
            if "exit_code" in summary:
                extra.append(f"exit={summary['exit_code']}")
            tail = f" [{', '.join(extra)}]" if extra else ""
            lines.append(
                f"  {summary['job_id']}  pid={summary['pid']:<6}  "
                f"{summary['status']:<14}  {summary['command']}{tail}"
            )
        return "\n".join(lines)

    def _render_status(self, job: BackgroundJob) -> str:
        s = job.to_summary()
        lines = [
            f"job_id:     {s['job_id']}",
            f"pid:        {s['pid']}",
            f"status:     {s['status']}",
            f"command:    {s['command']}",
            f"started_at: {s['started_at']}",
            f"log_dir:    {s['log_dir']}",
        ]
        if "elapsed_s" in s:
            lines.append(f"elapsed:    {s['elapsed_s']}s")
        if "exited_at" in s:
            lines.append(f"exited_at:  {s['exited_at']}")
        if "exit_code" in s:
            lines.append(f"exit_code:  {s['exit_code']}")
        if s.get("description"):
            lines.append(f"description:{s['description']}")
        return "\n".join(lines)

    def _render_tail(self, job: BackgroundJob, tail_lines: int) -> str:
        stdout_tail = _tail_file(job.stdout_path, tail_lines)
        stderr_tail = _tail_file(job.stderr_path, tail_lines)
        chunks = [f"job {job.job_id} ({job.status_label()})"]
        if stdout_tail:
            chunks.append(f"--- stdout (last {tail_lines} lines) ---\n{stdout_tail}")
        if stderr_tail:
            chunks.append(f"--- stderr (last {tail_lines} lines) ---\n{stderr_tail}")
        if not stdout_tail and not stderr_tail:
            chunks.append("(no output yet)")
        return "\n\n".join(chunks)

    async def _do_kill(self, job: BackgroundJob) -> str:
        if not job.running:
            return f"job {job.job_id} already exited (code={job.exit_code})."
        await job.kill()
        return f"job {job.job_id} terminated (exit={job.exit_code})."

    async def _do_wait(self, job: BackgroundJob, timeout: int, tail_lines: int) -> str:
        if not job.running:
            tail = _tail_file(job.stdout_path, tail_lines)
            tail_block = f"\n--- stdout tail ---\n{tail}" if tail else ""
            return (
                f"job {job.job_id} already exited (code={job.exit_code}, "
                f"runtime={job.exited_at - job.started_at:.1f}s).{tail_block}"
            )
        try:
            await asyncio.wait_for(job.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            tail = _tail_file(job.stdout_path, tail_lines)
            tail_block = f"\n--- stdout tail ---\n{tail}" if tail else ""
            elapsed = time.time() - job.started_at
            return (
                f"job {job.job_id} still running after {timeout}s "
                f"(elapsed={elapsed:.1f}s). Call bg(action='wait') again or "
                f"bg(action='kill') to terminate.{tail_block}"
            )
        # exit metadata is stamped by the reaper task; give it a beat.
        await asyncio.sleep(0)
        tail = _tail_file(job.stdout_path, tail_lines)
        tail_block = f"\n--- stdout tail ---\n{tail}" if tail else ""
        runtime = (
            (job.exited_at - job.started_at)
            if job.exited_at is not None
            else (time.time() - job.started_at)
        )
        return (
            f"job {job.job_id} exited (code={job.exit_code}, "
            f"runtime={runtime:.1f}s).{tail_block}"
        )


def cleanup_old_job_dirs(root: Path, *, keep: int = 50) -> None:
    """Best-effort prune of stale ``.mira/jobs/<job_id>`` log directories.

    Called opportunistically when starting a new background job so log dirs
    don't accumulate forever. Errors are swallowed because this is purely a
    housekeeping concern.
    """
    if not root.exists():
        return
    try:
        entries = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("bg-")]
    except OSError:
        return
    if len(entries) <= keep:
        return
    entries.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
    for stale in entries[: len(entries) - keep]:
        try:
            shutil.rmtree(stale, ignore_errors=True)
        except Exception:
            pass
