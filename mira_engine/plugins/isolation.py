"""Run experimental plugins out of process (design §3.5, mira#211).

An ``experimental`` plugin (or any plugin declaring ``isolation: subprocess``)
never runs inside the engine. Its tools are served by
:mod:`mira_engine.plugins.host` in a child process:

* **Sandboxed** (Linux + bwrap): the host sees system directories read-only,
  its own code read-only, a private scratch directory, and the user's
  workspace only if the manifest asks (``workspace:read`` / ``workspace:write``).
  No declared network → a private network namespace (no network at all). The
  Mira home — config, provider keys, the gateway token — is masked.
* **Minimal environment** either way: only ``PATH``, locale and Python path
  variables are passed, never the engine's API keys.
* **Crash/hang isolation**: a crashing host fails the call and is restarted on
  the next one; a hung call is killed at its timeout. The plugin's circuit
  breaker counts both.

Without bwrap the host can still run out of process (crashes and hangs stay
isolated) but permissions are not enforced, so that needs the user's explicit
``plugins.unsandboxed_experimental``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.agent.tools.base import Tool
from mira_engine.plugins.manifest import PluginManifest

_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH", "VIRTUAL_ENV", "SYSTEMROOT", "TMPDIR")
DESCRIBE_TIMEOUT_S = 30.0


class IsolationUnavailable(Exception):
    """The plugin can't run out of process here (no sandbox and no opt-in)."""


def host_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _scratch_dir(plugin_id: str) -> Path:
    from mira_engine.config.paths import get_data_dir

    path = get_data_dir() / "plugin-data" / plugin_id
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def host_argv(
    manifest: PluginManifest,
    *,
    workspace: Path | None,
    sandbox: bool,
    describe: bool = False,
) -> list[str]:
    cmd = [sys.executable, "-m", "mira_engine.plugins.host", *(["--describe"] if describe else []), str(manifest.root)]
    if not sandbox:
        return cmd
    import mira_engine
    from mira_engine.agent.tools.sandbox import SandboxSpec, bwrap_argv

    fs = manifest.permissions.filesystem
    read_only = [str(manifest.root)]
    read_write: list[str] = []
    if workspace is not None and fs == "workspace:read":
        read_only.append(str(workspace))
    if workspace is not None and fs == "workspace:write":
        read_write.append(str(workspace))
    spec = SandboxSpec.build(
        _scratch_dir(manifest.id),
        network="full" if manifest.permissions.network else "none",
        read_only_paths=read_only,
        read_write_paths=read_write,
        extra_read_only=[Path(mira_engine.__file__).resolve().parent],
    )
    return bwrap_argv(spec) + cmd


def plan(manifest: PluginManifest, *, workspace: Path | None, allow_unsandboxed: bool) -> bool:
    """Decide whether to sandbox. Returns True (sandboxed) / False (opted-in
    unsandboxed); raises :class:`IsolationUnavailable` otherwise."""
    from mira_engine.agent.tools.sandbox import sandbox_available

    ok, why = sandbox_available(network=bool(manifest.permissions.network))
    if ok:
        return True
    if allow_unsandboxed:
        logger.warning("plugin {} runs out of process WITHOUT a sandbox ({}); permissions are not enforced", manifest.id, why)
        return False
    raise IsolationUnavailable(
        f"needs the bwrap sandbox ({why}); set plugins.unsandboxed_experimental to run it unsandboxed"
    )


def describe(manifest: PluginManifest, *, workspace: Path | None, sandbox: bool) -> list[dict[str, Any]]:
    """Ask a one-shot host for the plugin's tool definitions."""
    argv = host_argv(manifest, workspace=workspace, sandbox=sandbox, describe=True)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=DESCRIBE_TIMEOUT_S, env=host_env(), check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"plugin host did not describe its tools within {DESCRIBE_TIMEOUT_S}s") from e
    line = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    try:
        data = json.loads(line[0])
    except ValueError as e:
        raise RuntimeError(f"plugin host failed (exit {proc.returncode}): {(proc.stderr or '').strip()[-400:]}") from e
    if "error" in data:
        raise RuntimeError(f"plugin host failed: {data['error']}")
    return list(data.get("tools") or [])


class PluginHostClient:
    """A long-lived host process, started lazily and restarted after a crash."""

    def __init__(self, plugin_id: str, argv: list[str]):
        self.plugin_id = plugin_id
        self.argv = argv
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._start_lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        async with self._start_lock:
            if self._proc is None or self._proc.returncode is not None:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=host_env(),
                    limit=16 * 1024 * 1024,
                )
                self._reader = asyncio.create_task(self._read(self._proc), name=f"plugin-host-{self.plugin_id}")
            return self._proc

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
        # The host exited: fail whatever was still waiting.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"plugin host for {self.plugin_id} exited"))
        self._pending.clear()

    async def call(self, tool: str, args: dict[str, Any], timeout: float) -> Any:
        proc = await self._ensure()
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        assert proc.stdin is not None
        proc.stdin.write((json.dumps({"id": rid, "method": "call", "params": {"tool": tool, "args": args}}) + "\n").encode())
        await proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # A hung plugin call: kill the host; the next call starts a fresh one.
            self._pending.pop(rid, None)
            await self.close()
            raise

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None


class SubprocessTool(Tool):
    """A tool whose implementation runs in the plugin's host process. Wrap it in
    :class:`~mira_engine.plugins.guard.GuardedTool` for timeouts and the breaker."""

    # Below GuardedTool's 60 s, so a hung host is killed here rather than the
    # call merely being cancelled from outside.
    def __init__(self, client: PluginHostClient, definition: dict[str, Any], call_timeout_s: float = 55.0):
        self._client = client
        self._def = definition
        self._call_timeout_s = call_timeout_s

    @property
    def name(self) -> str:
        return str(self._def["name"])

    @property
    def description(self) -> str:
        return str(self._def.get("description") or "")

    @property
    def parameters(self) -> dict[str, Any]:
        params = self._def.get("parameters")
        return params if isinstance(params, dict) else {"type": "object", "properties": {}}

    @property
    def read_only(self) -> bool:
        return bool(self._def.get("read_only"))

    async def execute(self, **kwargs: Any) -> Any:
        return await self._client.call(self.name, kwargs, self._call_timeout_s)
