"""Shell execution tool."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mira_engine.agent.tools.base import Tool
from mira_engine.agent.tools.bg import (
    BackgroundJobRegistry,
    cleanup_old_job_dirs,
    spawn_background_job,
)
from mira_engine.agent.tools.sandbox import wrap_command
from mira_engine.config.paths import get_media_dir
from mira_engine.security.network import contains_internal_url

if TYPE_CHECKING:
    from mira_engine.config.schema import PythonRuntimeConfig

logger = logging.getLogger(__name__)

_PYTHON_EXECUTABLE_NAMES = frozenset(
    {"python", "python3", "pip", "pip3", "pytest", "ipython", "jupyter", "uv"}
)
_SEGMENT_SEPARATOR_RE = re.compile(r"\s*(?:&&|\|\||[;|])\s*")
# Strips ``KEY=VAL `` env-var prefixes that appear at the head of a command
# segment in POSIX shells. Repeated to handle ``A=1 B=2 python x.py``.
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S+\s+")


_PIP_INSTALL_RE = re.compile(
    r"""
    (?P<lead>(?:^|(?<=&&\ )|(?<=\|\|\ )|(?<=;\ )|(?<=\|\ )))   # boundary
    (?P<prefix>(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)              # KEY=VAL prefixes
    (?P<head>
        (?:[\w./\\-]*pip3?(?:\.exe)?)                           # pip / pip3 (path or bare)
      | (?:[\w./\\-]*python3?(?:\.exe)?\s+-m\s+pip)             # python -m pip
    )
    \s+install\b                                                # the subcommand
    """,
    re.VERBOSE,
)


def rewrite_pip_install_to_uv(command: str) -> str:
    """Rewrite ``pip install`` (and ``python -m pip install``) into
    ``uv pip install`` for every command segment, preserving everything
    else (env-var prefixes, command chaining, the rest of the args).

    Read-only pip subcommands (``pip list``, ``pip show``, ``pip
    freeze``) are **not** rewritten — only ``install`` mutates state and
    benefits from uv.lock-aware routing.

    The function is text-based: it doesn't actually parse the shell
    grammar. It handles the common cases the agent is likely to emit:

    - ``pip install foo``
    - ``pip3 install foo``
    - ``python -m pip install foo``
    - ``./.venv/bin/pip install foo``
    - ``cd dir && pip install foo``
    - ``PIP_INDEX_URL=... pip install foo``

    Anything more exotic (subshells, here-docs, quoted ``pip install``
    inside a script literal) is left untouched on purpose; rewriting
    those is risk-greater than reward.
    """
    if not command or "install" not in command:
        return command

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix") or ""
        return f"{prefix}uv pip install"

    return _PIP_INSTALL_RE.sub(_replace, command)


def _is_python_command(command: str) -> bool:
    """Return True if ``command`` looks like it expects a Python interpreter.

    Detects bare ``python``, ``python3``, ``pip``, ``pytest``, ``ipython``,
    ``jupyter``, ``uv`` invocations as well as path-prefixed variants
    (``/usr/bin/python``, ``./venv/bin/python``) and chained commands
    (``cd foo && python x``, ``activate; pip install .``,
    ``PYTHONHASHSEED=0 python script.py``). Used to decide whether to
    lazily bootstrap the project venv before spawning the subprocess.

    Slight over-triggering is acceptable: bootstrap is idempotent and the
    second invocation short-circuits via :class:`ExecTool._venv_cache`.
    """
    if not command:
        return False
    for raw_segment in _SEGMENT_SEPARATOR_RE.split(command):
        segment = raw_segment.strip()
        if not segment:
            continue
        # Drop any leading ``KEY=VAL`` assignments (one or more).
        while True:
            stripped = _ENV_PREFIX_RE.sub("", segment)
            if stripped == segment:
                break
            segment = stripped
        first_token = segment.split(None, 1)[0] if segment else ""
        if not first_token:
            continue
        # Strip path components: ``/usr/bin/python3`` -> ``python3``.
        basename = first_token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        # Drop a trailing ``.exe`` so Windows paths still match.
        if basename.lower().endswith(".exe"):
            basename = basename[:-4]
        if basename in _PYTHON_EXECUTABLE_NAMES:
            return True
    return False

_IS_WINDOWS = sys.platform == "win32"
_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE")

# Runtime-relevant variables we transparently forward to subprocesses.
# Without these, an agent that runs ``python script.py`` either resolves
# ``python`` against bash login profiles (Unix) or fails to locate the
# correct interpreter inside a virtualenv / conda env that the engine itself
# was launched from. Sensitive markers above still apply on top of this list.
_UNIX_ENV_KEYS = (
    "HOME",
    "LANG",
    "TERM",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
    "TMPDIR",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "PYTHONUNBUFFERED",
    "PYTHONIOENCODING",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
)
_UNIX_ENV_PREFIXES = ("LC_", "MIRA_")  # locale + project meta we mint ourselves
# Windows core keys: always present in the subprocess env (defaulted if empty).
# Many Win32 APIs misbehave when these are unset entirely.
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
# Windows optional keys: only forwarded when actually set in the parent env.
# We avoid synthesising empty values for VIRTUAL_ENV / CONDA_PREFIX because
# Python launchers and conda activate scripts treat "" differently from unset.
_WINDOWS_OPTIONAL_KEYS = (
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "PYTHONUNBUFFERED",
    "PYTHONIOENCODING",
)
_WINDOWS_ENV_PREFIXES = ("MIRA_",)


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
        python_runtime: "PythonRuntimeConfig | None" = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self._runtime_working_dir: ContextVar[str | None] = ContextVar(
            "exec_runtime_working_dir",
            default=None,
        )
        # Background execution is opt-in: callers (the loop) wire a shared
        # registry and flip ``enable_background``. Subagents leave it off so
        # the LLM doesn't accidentally spawn fire-and-forget jobs in a
        # context that has no companion ``bg`` tool to inspect them.
        self.background_registry = background_registry
        self.enable_background = enable_background and background_registry is not None
        # Per-project Python runtime config. ``None`` and ``manager == "off"``
        # both mean "do not manage venvs"; the tool resolves ``python`` against
        # the parent process environment exactly like before.
        self.python_runtime = python_runtime
        # Per-project venv cache: working_dir -> resolved venv path (or None
        # to mean "bootstrap was attempted and failed; do not retry"). Keeps
        # the bootstrap subprocess off the hot path for repeated commands.
        self._venv_cache: dict[str, Path | None] = {}
        self._venv_cache_lock = asyncio.Lock()
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

    def set_runtime_context(self, *, workspace: Path, **_: Any) -> None:
        """Set the per-turn working directory used by a multi-project gateway."""

        self._runtime_working_dir.set(str(workspace))

    def clear_runtime_context(self) -> None:
        """Clear the per-turn working directory."""

        self._runtime_working_dir.set(None)

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
        cwd = working_dir or self._runtime_working_dir.get() or self.working_dir or os.getcwd()
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

        venv = await self._maybe_bootstrap_venv(command, cwd)
        env = self._build_env()
        if venv is not None:
            self._apply_venv_to_env(env, venv)
        spawn_command = command
        if venv is not None and self._should_rewrite_pip():
            spawn_command = rewrite_pip_install_to_uv(spawn_command)

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

    async def _maybe_bootstrap_venv(self, command: str, cwd: str) -> Path | None:
        """Lazily provision a project-local venv before running ``command``.

        Returns the resolved venv path if the configured manager is active,
        the command looks like it needs Python, and bootstrap succeeded.
        Returns ``None`` in all other cases — including when bootstrap fails;
        the caller should fall back to the legacy environment so a
        misconfigured uv install doesn't bring the agent to a halt.
        """
        runtime = self.python_runtime
        if runtime is None or runtime.manager != "uv":
            return None
        if not runtime.auto_bootstrap:
            return None
        if not _is_python_command(command):
            return None

        async with self._venv_cache_lock:
            if cwd in self._venv_cache:
                return self._venv_cache[cwd]

            try:
                venv = await asyncio.to_thread(self._bootstrap_venv_sync, cwd, runtime)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to bootstrap project venv at %s: %s; "
                    "falling back to system python.",
                    cwd,
                    exc,
                )
                self._venv_cache[cwd] = None
                return None

            self._venv_cache[cwd] = venv
            return venv

    @staticmethod
    def _bootstrap_venv_sync(
        cwd: str, runtime: "PythonRuntimeConfig"
    ) -> Path | None:
        """Synchronous bridge to :func:`ensure_project_venv`.

        Imported lazily so importing this module never requires
        :mod:`mira_engine.runtime` to be loadable (keeps the engine bootable
        on hosts without uv).
        """
        from mira_engine.runtime.python_env import ensure_project_venv

        return ensure_project_venv(cwd, runtime)

    def _should_rewrite_pip(self) -> bool:
        """True iff the active runtime config asked us to rewrite
        ``pip install`` into ``uv pip install``."""
        runtime = self.python_runtime
        if runtime is None or runtime.manager != "uv":
            return False
        return bool(getattr(runtime, "rewrite_pip_install", False))

    @staticmethod
    def _apply_venv_to_env(env: dict[str, str], venv: Path) -> None:
        """Mutate ``env`` so subprocesses see the venv as activated.

        Mirrors what ``source <venv>/bin/activate`` does: prepends the
        venv's ``bin/`` (or ``Scripts/``) to PATH, sets ``VIRTUAL_ENV``,
        and scrubs ``CONDA_*`` / ``PYTHONHOME`` so a coexisting conda
        activation doesn't shadow the venv's interpreter.
        """
        bin_name = "Scripts" if _IS_WINDOWS else "bin"
        venv_bin = str(venv / bin_name)
        # Use the platform's native PATH separator regardless of the
        # interpreter's ``os.pathsep`` (which only reflects the host OS,
        # not the simulated platform under test).
        pathsep = ";" if _IS_WINDOWS else ":"
        existing_path = env.get("PATH", "")
        env["PATH"] = (
            venv_bin + pathsep + existing_path if existing_path else venv_bin
        )
        env["VIRTUAL_ENV"] = str(venv)
        env.pop("CONDA_PREFIX", None)
        env.pop("CONDA_DEFAULT_ENV", None)
        env.pop("PYTHONHOME", None)

    def _build_env(self) -> dict[str, str]:
        """Build a curated subprocess environment.

        Forwards a positive allowlist of runtime-relevant variables (PATH,
        locale, virtualenv / conda activation hints, native library search
        paths, etc.) while still scrubbing anything that looks like a credential
        via :data:`_SENSITIVE_ENV_MARKERS`.
        """
        if _IS_WINDOWS:
            env: dict[str, str] = {}
            for key in _WINDOWS_ENV_KEYS:
                env[key] = os.environ.get(key) or ""
            env["SYSTEMROOT"] = env["SYSTEMROOT"] or r"C:\Windows"
            env["COMSPEC"] = env["COMSPEC"] or "cmd.exe"
            env["USERPROFILE"] = env["USERPROFILE"] or r"C:\Users\Default"
            env["HOMEDRIVE"] = env["HOMEDRIVE"] or "C:"
            env["HOMEPATH"] = env["HOMEPATH"] or r"\Users\Default"
            env["TEMP"] = env["TEMP"] or r"C:\Windows\Temp"
            env["TMP"] = env["TMP"] or env["TEMP"]
            env["PATHEXT"] = env["PATHEXT"] or ".COM;.EXE;.BAT;.CMD"
            env["PATH"] = env["PATH"] or r"C:\Windows\System32;C:\Windows"
            for key in _WINDOWS_OPTIONAL_KEYS:
                value = os.environ.get(key)
                if value:
                    env[key] = value
            for name, value in os.environ.items():
                if any(name.startswith(prefix) for prefix in _WINDOWS_ENV_PREFIXES):
                    env.setdefault(name, value)
            return self._scrub_sensitive(env)

        env: dict[str, str] = {
            "HOME": os.environ.get("HOME", str(Path.home())),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "xterm-256color"),
        }
        for key in _UNIX_ENV_KEYS:
            if key in env:
                continue
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        for name, value in os.environ.items():
            if any(name.startswith(prefix) for prefix in _UNIX_ENV_PREFIXES):
                env.setdefault(name, value)
        return self._scrub_sensitive(env)

    @staticmethod
    def _scrub_sensitive(env: dict[str, str]) -> dict[str, str]:
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
