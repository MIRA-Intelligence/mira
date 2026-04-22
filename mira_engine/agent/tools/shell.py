"""Shell execution tool."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from mira_engine.agent.tools.base import Tool
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
    ):
        self.timeout = timeout
        self.working_dir = working_dir
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
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }
    
    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        timeout = kwargs.get("timeout", self.timeout)
        try:
            timeout = int(timeout)
        except Exception:
            timeout = self.timeout
        timeout = max(1, min(timeout, self._MAX_TIMEOUT))
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        env = self._build_env()
        spawn_command = command

        if self.path_append:
            if _IS_WINDOWS:
                env["PATH"] = (env.get("PATH", "") + ";" + self.path_append).strip(";")
            else:
                spawn_command = f'export PATH="$PATH:{self.path_append}" && {spawn_command}'

        if self.sandbox and self.sandbox == "bwrap" and not _IS_WINDOWS:
            spawn_command = wrap_command(self.sandbox, spawn_command, cwd, cwd)

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
