"""Run experimental plugins out of process (design §3.5, mira#211).

An ``experimental`` plugin (or any plugin declaring ``isolation: subprocess``)
never runs inside the engine. Its tools are served by
:mod:`mira_engine.plugins.host` in a child process:

* **Sandboxed** — bwrap on Linux, Seatbelt (``sandbox-exec``) on macOS: the
  host can read its own code, its interpreter and a private scratch directory
  (also its temp dir), and the user's workspace only if the manifest asks
  (``workspace:read`` / ``workspace:write``); it writes only to the scratch dir
  and a ``workspace:write`` workspace. No declared network → no network. The
  Mira home — config, provider keys, the gateway token — stays hidden (on
  macOS the whole home directory does, except those grants). Without
  ``shell: true`` it can't start other programs (enforced by Seatbelt on macOS
  and by the permission tripwire inside the host everywhere).
* **Minimal environment** either way: only ``PATH``, locale, Python path and
  the OS's own variables are passed, never the engine's API keys. On Windows
  the host's ``USERPROFILE`` is its scratch dir, so the real profile stays
  hidden and ``Path.home()`` still works.
* **Crash/hang isolation**: a crashing host fails the call and is restarted on
  the next one; a hung call is killed at its timeout. The plugin's circuit
  breaker counts both.

Without a sandbox (Windows, or Linux without working bwrap) the host can
still run out of process (crashes and hangs stay isolated) but permissions
are only checked by the in-host tripwire, so that needs the user's explicit
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
from mira_engine.plugins.context import plugin_data_dir
from mira_engine.plugins.manifest import PluginManifest

_ENV_KEEP = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH", "VIRTUAL_ENV", "TMPDIR",
    # Windows: without these a Python child cannot start or find its DLLs.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
)
_SITE_DIRS = ("site-packages", "dist-packages")
DESCRIBE_TIMEOUT_S = 30.0


class IsolationUnavailable(Exception):
    """The plugin can't run out of process here (no sandbox and no opt-in)."""


def host_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if not getattr(sys, "frozen", False):
        # The host must run this engine's code, wherever it was imported from
        # (a source checkout, another install): it doesn't start in the
        # engine's working directory. An installed engine is found through
        # the interpreter's own site dirs; putting those on PYTHONPATH would
        # rank them above the standard library.
        import mira_engine

        root = Path(mira_engine.__file__).resolve().parent.parent
        if root.name not in _SITE_DIRS:
            env["PYTHONPATH"] = os.pathsep.join([str(root), *[p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]])
    return env


def plugin_env(
    manifest: PluginManifest,
    *,
    workspace: Path | None,
    config: dict[str, Any],
    tier: str | None,
) -> dict[str, str]:
    """The host's environment: :func:`host_env` plus what the plugin's context
    is built from (see ``mira_engine.plugins.host``). Only the config values
    the manifest declared in ``permissions.secrets`` are passed, and the
    workspace only when ``permissions.filesystem`` grants it."""
    from mira_engine.config.paths import get_data_dir

    env = host_env()
    scratch = str(plugin_data_dir(manifest.id))
    env["MIRA_PLUGIN_DATA_DIR"] = scratch
    env["TMPDIR"] = scratch  # its temp files stay in its own (writable) dir
    if sys.platform == "win32":
        env["USERPROFILE"] = scratch  # pathlib's home; the real profile stays hidden
    env["MIRA_PLUGIN_MIRA_HOME"] = str(get_data_dir())
    env["MIRA_PLUGIN_TIER"] = tier or "experimental"
    if workspace is not None and manifest.permissions.filesystem != "none":
        env["MIRA_PLUGIN_WORKSPACE"] = str(workspace)
    if config:
        env["MIRA_PLUGIN_CONFIG"] = json.dumps(config, default=str)
    return env


def host_argv(
    manifest: PluginManifest,
    *,
    workspace: Path | None,
    sandbox: str | bool | None,
    describe: bool = False,
) -> list[str]:
    from mira_engine.plugins.paths import host_command

    cmd = [*host_command(), *(["--describe"] if describe else []), str(manifest.root)]
    if not sandbox:
        return cmd
    import mira_engine
    from mira_engine.agent.tools.sandbox import SandboxSpec, bwrap_argv, seatbelt_argv

    fs = manifest.permissions.filesystem
    read_only = [str(manifest.root)]
    read_write: list[str] = []
    if workspace is not None and fs == "workspace:read":
        read_only.append(str(workspace))
    if workspace is not None and fs == "workspace:write":
        read_write.append(str(workspace))
    spec = SandboxSpec.build(
        plugin_data_dir(manifest.id),
        network="full" if manifest.permissions.network else "none",
        read_only_paths=read_only,
        read_write_paths=read_write,
        extra_read_only=[Path(mira_engine.__file__).resolve().parent],
    )
    if sandbox == "seatbelt":
        # bwrap shows only what it mounts; Seatbelt hides by listing, so an
        # ungranted workspace outside the home is hidden explicitly.
        hide = [workspace] if workspace is not None and fs == "none" else []
        return seatbelt_argv(spec, shell=manifest.permissions.shell, hide=hide) + cmd
    return bwrap_argv(spec) + cmd


def plan(manifest: PluginManifest, *, workspace: Path | None, allow_unsandboxed: bool) -> str | None:
    """Pick the sandbox: ``"bwrap"`` / ``"seatbelt"``, or None (opted-in
    unsandboxed); raises :class:`IsolationUnavailable` otherwise."""
    from mira_engine.agent.tools.sandbox import plugin_sandbox

    backend, why = plugin_sandbox(network=bool(manifest.permissions.network))
    if backend:
        return backend
    if allow_unsandboxed:
        logger.warning(
            "plugin {} runs out of process WITHOUT a sandbox ({}); only the in-host tripwire checks its permissions",
            manifest.id, why,
        )
        return None
    raise IsolationUnavailable(
        f"needs a sandbox ({why}); set plugins.unsandboxed_experimental to run it unsandboxed"
    )


def describe(
    manifest: PluginManifest,
    *,
    workspace: Path | None,
    sandbox: str | bool | None,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Ask a one-shot host for the plugin's tool definitions."""
    argv = host_argv(manifest, workspace=workspace, sandbox=sandbox, describe=True)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=DESCRIBE_TIMEOUT_S, env=env or host_env(),
            cwd=plugin_data_dir(manifest.id), check=False,
        )
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

    def __init__(self, plugin_id: str, argv: list[str], env: dict[str, str] | None = None, cwd: Path | None = None):
        self.plugin_id = plugin_id
        self.argv = argv
        self.env = env
        # The host works in its scratch dir: the engine's working directory
        # may be one the sandbox hides (anything in the home on macOS).
        self.cwd = cwd
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
                    env=self.env or host_env(),
                    cwd=self.cwd,
                    limit=16 * 1024 * 1024,
                )
                self._reader = asyncio.create_task(self._read(self._proc), name=f"plugin-host-{self.plugin_id}")
                # Wait until the host has imported the engine and the plugin,
                # so a cold start doesn't eat into the first call's timeout.
                try:
                    await self._request(self._proc, "describe", None, DESCRIBE_TIMEOUT_S)
                except BaseException:
                    await self.close()
                    raise
            return self._proc

    async def _request(
        self, proc: asyncio.subprocess.Process, method: str, params: dict[str, Any] | None, timeout: float
    ) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

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
        try:
            return await self._request(proc, "call", {"tool": tool, "args": args}, timeout)
        except asyncio.TimeoutError:
            # A hung plugin call: kill the host; the next call starts a fresh one.
            await self.close()
            raise

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        # Any other call still waiting on this host fails now, not at its own timeout.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"plugin host for {self.plugin_id} exited"))
        self._pending.clear()
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass


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
