"""Shell execution tool."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from mira_engine.agent.tools.base import Tool
from mira_engine.agent.tools.bg import (
    BackgroundJobRegistry,
    cleanup_old_job_dirs,
    spawn_background_job,
)
from mira_engine.agent.tools.sandbox import wrap_command
from mira_engine.config.paths import get_media_dir
from mira_engine.security.network import contains_internal_url

_IS_WINDOWS = sys.platform == "win32"
_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE")
_UNIX_ENV_KEYS = ("HOME", "LANG", "TERM")
_WINDOWS_ENV_KEYS = (
    "SYSTEMROOT",
    "COMSPEC",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "PATHEXT",
    "PATH",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
)


class ExecTool(Tool):
    """Tool to execute shell commands."""
    _MAX_OUTPUT = 10_000
    _MAX_TIMEOUT = 600

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        sandbox: str | None = None,
        background_registry: BackgroundJobRegistry | None = None,
        enable_background: bool = False,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        # Background execution is opt-in: callers (the loop) wire a shared
        # registry and flip ``enable_background``. Subagents leave it off so
        # the LLM doesn't accidentally spawn fire-and-forget jobs in a
        # context that has no companion ``bg`` tool to inspect them.
        self.background_registry = background_registry
        self.enable_background = enable_background and background_registry is not None
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.sandbox = sandbox

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        base = "Execute a shell command and return its output. Use with caution."
        if self.enable_background:
            base += (
                " Set background=true for long-running tasks (e.g. neural-net "
                "training): the command is launched as a detached subprocess, "
                "logs go to .mira/jobs/<job_id>/, and the call returns "
                "immediately with a job_id. Use the `bg` tool to poll, tail, "
                "wait, or kill it."
            )
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "command": {
                "type": "string",
                "description": "The shell command to execute"
            },
            "working_dir": {
                "type": "string",
                "description": "Optional working directory for the command"
            }
        }
        if self.enable_background:
            props["background"] = {
                "type": "boolean",
                "description": (
                    "If true, launch the command as a detached background job "
                    "and return immediately with a job_id (foreground timeout "
                    "does not apply). Monitor / control the job via the `bg` "
                    "tool. Use this for any command that may run longer than "
                    "a few minutes (model training, large preprocessing, "
                    "long simulations)."
                ),
            }
            props["description"] = {
                "type": "string",
                "description": (
                    "Optional human-readable label for the background job, "
                    "shown in `bg list`. Ignored when background=false."
                ),
            }
        return {
            "type": "object",
            "properties": props,
            "required": ["command"],
        }

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        background = bool(kwargs.get("background", False))
        timeout = kwargs.get("timeout", self.timeout)
        try:
            timeout = int(timeout)
        except Exception:
            timeout = self.timeout
        timeout = max(1, min(timeout, self._MAX_TIMEOUT))
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        if background:
            if not self.enable_background:
                return (
                    "Error: background execution is not enabled in this context. "
                    "Re-run with background=false (foreground), or escalate to "
                    "the main loop where the `bg` tool is available."
                )

        env = self._build_env()
        spawn_command = command

        if self.path_append:
            if _IS_WINDOWS:
                env["PATH"] = (env.get("PATH", "") + ";" + self.path_append).strip(";")
            else:
                spawn_command = f'export PATH="$PATH:{self.path_append}" && {spawn_command}'

        if self.sandbox and self.sandbox == "bwrap" and not _IS_WINDOWS:
            spawn_command = wrap_command(self.sandbox, spawn_command, cwd, cwd)

        if background:
            return await self._launch_background(
                spawn_command=spawn_command,
                cwd=cwd,
                env=env,
                description=kwargs.get("description"),
            )

        create_shell = getattr(asyncio, "create_subprocess_shell", None)
        if create_shell is not None and self.sandbox != "bwrap":
            try:
                await create_shell(
                    "true" if not _IS_WINDOWS else "ver",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            except Exception as e:
                return f"Error executing command: {str(e)}"

        try:
            process = await self._spawn(spawn_command, cwd, env)
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                # Wait for the process to fully terminate so pipes are
                # drained and file descriptors are released.
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {timeout} seconds"
            
            output_parts = []
            
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            
            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")
            
            result = "\n".join(output_parts) if output_parts else "(no output)"
            
            # Truncate very long output
            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                head = result[: max_len // 2]
                tail = result[-(max_len // 2) :]
                removed = len(result) - len(head) - len(tail)
                result = (
                    f"{head}\n... ({removed} chars truncated) ...\n{tail}"
                )
            
            return result
            
        except Exception as e:
            return f"Error executing command: {str(e)}"

    async def _launch_background(
        self,
        *,
        spawn_command: str,
        cwd: str,
        env: dict[str, str],
        description: str | None,
    ) -> str:
        """Spawn a detached subprocess and register it with the bg registry.

        We don't apply ``_MAX_TIMEOUT`` here — that's the whole point of the
        background path. The subprocess survives across agent loop iterations
        until it exits naturally, the agent kills it via ``bg(action='kill')``,
        or the loop shuts down (which best-effort terminates everything).
        """
        assert self.background_registry is not None  # guarded by enable_background
        jobs_root = Path(cwd) / ".mira" / "jobs"
        cleanup_old_job_dirs(jobs_root)
        try:
            job = await spawn_background_job(
                registry=self.background_registry,
                command=spawn_command,
                cwd=cwd,
                env=env,
                description=description,
                job_dir_root=jobs_root,
            )
        except Exception as e:
            return f"Error launching background job: {e}"
        return (
            f"Started background job {job.job_id} (pid={job.pid}).\n"
            f"Logs: {job.log_dir}\n"
            f"Use bg(action='status', job_id='{job.job_id}') or "
            f"bg(action='wait', job_id='{job.job_id}', timeout=...) to monitor."
        )

    def _build_env(self) -> dict[str, str]:
        """Build a minimized environment without sensitive parent variables."""
        if _IS_WINDOWS:
            env: dict[str, str] = {}
            for key in _WINDOWS_ENV_KEYS:
                value = os.environ.get(key)
                env[key] = value or ""
            env["SYSTEMROOT"] = env.get("SYSTEMROOT") or r"C:\Windows"
            env["COMSPEC"] = env.get("COMSPEC") or "cmd.exe"
            env["USERPROFILE"] = env.get("USERPROFILE") or r"C:\Users\Default"
            env["HOMEDRIVE"] = env.get("HOMEDRIVE") or "C:"
            env["HOMEPATH"] = env.get("HOMEPATH") or r"\Users\Default"
            env["TEMP"] = env.get("TEMP") or r"C:\Windows\Temp"
            env["TMP"] = env.get("TMP") or env["TEMP"]
            env["PATHEXT"] = env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
            env["PATH"] = env.get("PATH") or r"C:\Windows\System32;C:\Windows"
            return env

        env = {
            "HOME": os.environ.get("HOME", str(Path.home())),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "xterm-256color"),
        }
        return {
            key: value
            for key, value in env.items()
            if not any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS)
        }

    @staticmethod
    async def _spawn(command: str, cwd: str, env: dict[str, str]) -> asyncio.subprocess.Process:
        """Spawn platform-specific shell process."""
        if _IS_WINDOWS:
            comspec = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
            return await asyncio.create_subprocess_exec(
                comspec,
                "/c",
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        return await asyncio.create_subprocess_exec(
            "bash",
            "-l",
            "-c",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if re.search(r"(?:^|\s)\.\.(?:$|\s|/|\\)", cmd) or "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()
            media_path = get_media_dir().resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    p = Path(raw.strip()).expanduser().resolve()
                except Exception:
                    continue
                if (
                    p.is_absolute()
                    and cwd_path not in p.parents
                    and p != cwd_path
                    and media_path not in p.parents
                    and p != media_path
                ):
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\(?:[^\s\"'|><;]+)?", command)   # Windows: C:\...
        posix_paths = re.findall(r"(?:^|[\s|>\"])(/[^\s\"'>]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"~(?:/[^\s\"'|><;]+)?", command)
        return win_paths + posix_paths + home_paths
