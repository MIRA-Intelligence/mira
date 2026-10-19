"""The spawner interface and the shared launcher-command implementation.

Both spawners run the Remote-SSH commands (``mira remote attach|status|stop
--json``, see ``docs/remote-ssh.md``) as the target user and parse the single
JSON line they print. A user therefore has one engine (the engine lease, #170)
whether Remote-SSH or Mira Server started it.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from typing import Any

from mira_server.auth.identity import Identity

log = logging.getLogger(__name__)

REMOTE_PROTOCOL = 1
# start/status/stop map to these ``mira remote`` arguments. The sudo launcher
# embeds the same table, so it never takes arguments from Mira Server.
ACTION_ARGS: dict[str, tuple[str, ...]] = {
    "start": ("attach", "--json", "--timeout", "60"),
    "status": ("status", "--json"),
    "stop": ("stop", "--json"),
}
_MAX_OUTPUT = 64 * 1024

# Error codes surfaced by the spawner (engine state "conflict" for CONFLICT,
# "error" for the rest).
CONFLICT = "engine_conflict"
START_FAILED = "start_failed"
HEALTH_TIMEOUT = "health_timeout"
DENIED = "launcher_denied"
TIMEOUT = "launcher_timeout"
PROTOCOL_MISMATCH = "protocol_mismatch"
INTERNAL = "internal"


@dataclass(frozen=True)
class EngineHandle:
    port: int
    token: str
    pid: int | None
    version: str | None
    started: bool
    mira_home: str | None = None


class SpawnerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def conflict(self) -> bool:
        return self.code == CONFLICT


class Spawner(abc.ABC):
    """Start, inspect and stop one engine per user."""

    @abc.abstractmethod
    async def start(self, identity: Identity) -> EngineHandle:
        """Start or attach to the user's engine."""

    @abc.abstractmethod
    async def status(self, identity: Identity) -> EngineHandle | None:
        """The running engine, or None. Raises ``engine_conflict`` for one on another host."""

    @abc.abstractmethod
    async def stop(self, identity: Identity) -> None:
        """Stop the user's engine (a no-op when none is running)."""

    @abc.abstractmethod
    def expected_uid(self, identity: Identity) -> int:
        """The uid the user's engine must run as."""


def _map_remote_error(code: str, message: str) -> SpawnerError:
    if code == "lease_conflict":
        return SpawnerError(CONFLICT, message)
    if code == "config_invalid" and message.startswith("the running engine"):
        # An engine holds the lease but cannot be attached to (another token or
        # a non-loopback bind). Stopping it is how the user resolves this.
        return SpawnerError(CONFLICT, message)
    if code == "health_timeout":
        return SpawnerError(HEALTH_TIMEOUT, message)
    if code == "listener_mismatch":
        log.warning("security: %s", message)
        return SpawnerError(START_FAILED, message)
    if code in {"start_failed", "config_invalid"}:
        return SpawnerError(START_FAILED, message)
    return SpawnerError(INTERNAL, message or code)


_SUDO_DENIED_MARKERS = ("a password is required", "is not allowed to execute", "not in the sudoers",
                        "is not allowed to run sudo", "unknown user", "may not run sudo")


def parse_remote_output(stdout: bytes, stderr: bytes, returncode: int | None) -> dict[str, Any]:
    """Parse the one JSON line from ``mira remote ... --json``; raise :class:`SpawnerError`."""
    text = stdout.decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    payload: Any = None
    if lines:
        try:
            payload = json.loads(lines[-1])
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        err = stderr.decode("utf-8", errors="replace").strip()
        lowered = err.lower()
        if any(marker in lowered for marker in _SUDO_DENIED_MARKERS):
            raise SpawnerError(DENIED, "sudo refused to run the engine launcher for this user; "
                                       "check the sudoers rule and the user's group membership")
        last = err.splitlines()[-1] if err else f"exit code {returncode}"
        raise SpawnerError(START_FAILED, f"the engine launcher printed no result ({last[:300]})")
    if payload.get("protocol") != REMOTE_PROTOCOL:
        raise SpawnerError(PROTOCOL_MISMATCH,
                           f"the engine speaks remote protocol {payload.get('protocol')!r}, "
                           f"Mira Server needs {REMOTE_PROTOCOL}")
    if payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        raise _map_remote_error(str(error.get("code") or "internal"), str(error.get("message") or ""))
    return payload


def handle_from_payload(payload: dict[str, Any], *, started: bool | None = None) -> EngineHandle:
    port, token = payload.get("port"), payload.get("token")
    if not isinstance(port, int) or not 0 < port < 65536:
        raise SpawnerError(INTERNAL, "the engine launcher reported no valid port")
    if not isinstance(token, str) or not token:
        raise SpawnerError(INTERNAL, "the engine launcher reported no token")
    pid = payload.get("pid")
    version = payload.get("version")
    return EngineHandle(
        port=port,
        token=token,
        pid=pid if isinstance(pid, int) else None,
        version=version if isinstance(version, str) else None,
        started=bool(payload.get("started")) if started is None else started,
        mira_home=payload.get("mira_home") if isinstance(payload.get("mira_home"), str) else None,
    )


class LauncherSpawner(Spawner):
    """Runs a command that executes ``mira remote <action> --json`` as the user."""

    def __init__(self, *, start_timeout: float, stop_timeout: float) -> None:
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        # Launchers still running after printing their result: for `start` in
        # a logind session the launcher keeps the session open while the
        # engine runs. Reaped in the background.
        self._held: set[asyncio.Task] = set()

    @abc.abstractmethod
    def command(self, identity: Identity, action: str) -> tuple[list[str], dict[str, str]]:
        """argv and environment for ``action`` (start, status or stop)."""

    async def _run(self, identity: Identity, action: str, timeout: float) -> dict[str, Any]:
        argv, env = self.command(identity, action)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise SpawnerError(INTERNAL, f"cannot run the engine launcher: {exc}") from exc
        if action == "start":
            return await self._read_result(proc, identity, timeout)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            _kill_group(proc)
            await proc.wait()
            raise SpawnerError(TIMEOUT, f"the engine launcher did not finish {action} within {timeout:.0f}s") \
                from None
        except asyncio.CancelledError:
            _kill_group(proc)
            raise
        if stderr:
            log.debug("launcher %s for %s stderr: %s", action, identity.name,
                      stderr[-2000:].decode("utf-8", errors="replace"))
        return parse_remote_output(stdout[-_MAX_OUTPUT:], stderr[-_MAX_OUTPUT:], proc.returncode)

    async def _read_result(self, proc: asyncio.subprocess.Process, identity: Identity,
                           timeout: float) -> dict[str, Any]:
        """Read up to the launcher's JSON result line without waiting for it to exit."""
        assert proc.stdout is not None and proc.stderr is not None
        stderr = bytearray()

        async def drain_stderr() -> None:
            while chunk := await proc.stderr.read(4096):
                if len(stderr) < _MAX_OUTPUT:
                    stderr.extend(chunk)

        stderr_task = asyncio.create_task(drain_stderr())
        lines: list[bytes] = []
        try:
            async with asyncio.timeout(timeout):
                while line := await proc.stdout.readline():
                    lines.append(line)
                    if _is_result_line(line):
                        break
                else:
                    await proc.wait()
                    await stderr_task
        except TimeoutError:
            _kill_group(proc)
            await proc.wait()
            stderr_task.cancel()
            raise SpawnerError(TIMEOUT, f"the engine launcher did not finish start within {timeout:.0f}s") \
                from None
        except asyncio.CancelledError:
            _kill_group(proc)
            stderr_task.cancel()
            raise
        if stderr:
            log.debug("launcher start for %s stderr: %s", identity.name,
                      bytes(stderr[-2000:]).decode("utf-8", errors="replace"))
        if proc.returncode is None:
            task = asyncio.create_task(self._reap(proc, stderr_task))
            self._held.add(task)
            task.add_done_callback(self._held.discard)
        return parse_remote_output(b"".join(lines)[-_MAX_OUTPUT:], bytes(stderr), proc.returncode)

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process, stderr_task: asyncio.Task) -> None:
        try:
            await proc.wait()
        finally:
            stderr_task.cancel()

    async def start(self, identity: Identity) -> EngineHandle:
        payload = await self._run(identity, "start", self.start_timeout)
        return handle_from_payload(payload)

    async def status(self, identity: Identity) -> EngineHandle | None:
        payload = await self._run(identity, "status", self.stop_timeout)
        if not payload.get("running"):
            return None
        if "token" not in payload:
            raise SpawnerError(CONFLICT, f"the engine runs on host {payload.get('hostname')}; stop it there first")
        return handle_from_payload(payload, started=False)

    async def stop(self, identity: Identity) -> None:
        await self._run(identity, "stop", self.stop_timeout)


def _is_result_line(line: bytes) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
