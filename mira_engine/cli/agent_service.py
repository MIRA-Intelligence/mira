"""CLI entrypoint for local Mira engine service lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import typer
from rich.console import Console

app = typer.Typer(
    name="mira-engine",
    help="Manage local Mira engine service lifecycle.",
    no_args_is_help=True,
)
console = Console()

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_INSTALLED = 2
LAUNCHD_LABEL = "com.projectmira.engine"
SYSTEMD_UNIT_NAME = "mira-engine.service"
WINDOWS_SERVICE_NAME = "MiraEngine"
WINDOWS_SERVICE_DISPLAY_NAME = "Mira Engine"
WINDOWS_SERVICE_WRAPPER_NAME = "MiraEngineService.exe"
WINDOWS_SERVICE_CONFIG_NAME = "MiraEngineService.xml"
DEFAULT_PORT = 18790
LOG_ROTATE_BYTES = 1_000_000
LOG_ROTATE_FILES = 3
DIAGNOSTICS_LOG_TAIL_LINES = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _engine_manifest_path() -> Path:
    return Path(sys.executable).with_name("mira-engine.manifest.json")


def _load_engine_manifest() -> dict[str, Any] | None:
    path = _engine_manifest_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _current_engine_identity() -> dict[str, Any]:
    executable = Path(sys.executable)
    manifest = _load_engine_manifest()
    identity: dict[str, Any] = {
        "engine_executable": str(executable),
        "engine_manifest_path": str(_engine_manifest_path()),
    }
    if manifest is not None:
        identity["engine_manifest"] = manifest
        identity["engine_sha256"] = manifest.get("sha256")
        return identity

    identity["engine_manifest"] = None
    identity["engine_sha256"] = _sha256_file(executable)
    return identity


@dataclass
class AgentPaths:
    root: Path
    config_dir: Path
    data_dir: Path
    logs_dir: Path
    runtime_dir: Path
    state_file: Path
    log_file: Path
    launchd_plist: Path
    systemd_unit: Path
    backups_dir: Path

    @classmethod
    def default(cls) -> "AgentPaths":
        from mira_engine.config.loader import get_home_dir

        return cls._build(home=Path.home(), root=get_home_dir())

    @classmethod
    def for_home(cls, home: Path) -> "AgentPaths":
        home_path = home.expanduser()
        return cls._build(home=home_path, root=home_path / ".mira")

    @classmethod
    def _build(cls, *, home: Path, root: Path) -> "AgentPaths":
        home_path = home.expanduser()
        root = root.expanduser()
        return cls(
            root=root,
            config_dir=root / "config",
            data_dir=root / "data",
            logs_dir=root / "logs",
            runtime_dir=root / "runtime",
            state_file=root / "runtime" / "agent-service-state.json",
            log_file=root / "logs" / "agent-service.log",
            launchd_plist=home_path / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
            systemd_unit=home_path / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME,
            backups_dir=root / "runtime" / "backups",
        )

    def ensure(self) -> None:
        for path in (self.config_dir, self.data_dir, self.logs_dir, self.runtime_dir, self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.log_file.touch(exist_ok=True)


class LocalServiceManager:
    """File-backed lifecycle manager used as the portable default."""

    SERVICE_MODE = "local-skeleton"

    def __init__(self, paths: AgentPaths) -> None:
        self.paths = paths

    def _rotate_log_if_needed(self) -> None:
        if not self.paths.log_file.exists():
            return
        if self.paths.log_file.stat().st_size < LOG_ROTATE_BYTES:
            return

        for idx in range(LOG_ROTATE_FILES - 1, 0, -1):
            src = self.paths.log_file.with_name(f"{self.paths.log_file.name}.{idx}")
            dst = self.paths.log_file.with_name(f"{self.paths.log_file.name}.{idx + 1}")
            if src.exists():
                src.replace(dst)
        self.paths.log_file.replace(self.paths.log_file.with_name(f"{self.paths.log_file.name}.1"))
        self.paths.log_file.touch(exist_ok=True)

    def _append_log(self, event: str, **details: Any) -> None:
        self.paths.ensure()
        self._rotate_log_if_needed()
        payload = {
            "timestamp": _now_iso(),
            "event": event,
            "service_mode": self._default_state().get("service_mode"),
            "platform": platform.system().lower(),
            **details,
        }
        with self.paths.log_file.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _default_state(self) -> dict[str, Any]:
        return {
            "installed": False,
            "running": False,
            "service_mode": self.SERVICE_MODE,
            "platform": platform.system().lower(),
            "host": "127.0.0.1",
            "port": DEFAULT_PORT,
            "installed_at": None,
            "last_started_at": None,
            "last_stopped_at": None,
            "pid": None,
        }

    def load_state(self) -> dict[str, Any]:
        if not self.paths.state_file.exists():
            return self._default_state()
        try:
            payload = json.loads(self.paths.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        state = self._default_state()
        if isinstance(payload, dict):
            state.update(payload)
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        self.paths.ensure()
        self.paths.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def install_service(
        self,
        host: str | None = None,
        port: int | None = None,
        home: str | None = None,
        config_path: str | None = None,
    ) -> tuple[int, str]:
        state = self.load_state()
        self.paths.ensure()
        state["installed"] = True
        state["installed_at"] = state.get("installed_at") or _now_iso()
        if host is not None:
            state["host"] = host
        if port is not None:
            state["port"] = port
        if home is not None:
            state["home"] = str(Path(home).expanduser())
        if config_path is not None:
            state["config_path"] = str(Path(config_path).expanduser())
        state.update(_current_engine_identity())
        self.save_state(state)
        self._append_log("install_service", installed=True)
        return EXIT_OK, "service metadata installed"

    def uninstall_service(self) -> tuple[int, str]:
        state = self.load_state()
        state["installed"] = False
        state["running"] = False
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
        self._append_log("uninstall_service", installed=False)
        return EXIT_OK, "service metadata removed"

    def start(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            self._append_log("start_service_failed", reason="not_installed")
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        state["running"] = True
        state["last_started_at"] = _now_iso()
        self.save_state(state)
        self._append_log("start_service", running=True)
        return EXIT_OK, "service marked as running"

    def stop(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            self._append_log("stop_service_failed", reason="not_installed")
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        state["running"] = False
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
        self._append_log("stop_service", running=False)
        return EXIT_OK, "service marked as stopped"

    def status(self) -> tuple[int, dict[str, Any]]:
        state = self.load_state()
        return EXIT_OK, {
            "installed": bool(state.get("installed")),
            "running": bool(state.get("running")),
            "service_mode": state.get("service_mode"),
            "platform": state.get("platform"),
            "port": state.get("port"),
            "log_file": str(self.paths.log_file),
            "state_file": str(self.paths.state_file),
            "installed_at": state.get("installed_at"),
            "last_started_at": state.get("last_started_at"),
            "last_stopped_at": state.get("last_stopped_at"),
            "engine_executable": state.get("engine_executable"),
            "engine_manifest_path": state.get("engine_manifest_path"),
            "engine_manifest": state.get("engine_manifest"),
            "engine_sha256": state.get("engine_sha256"),
        }

    def doctor(self) -> tuple[int, dict[str, Any]]:
        self.paths.ensure()
        status_code, status_payload = self.status()
        checks = {
            "python_executable": bool(sys.executable),
            "config_dir_writable": self.paths.config_dir.exists(),
            "data_dir_writable": self.paths.data_dir.exists(),
            "logs_dir_writable": self.paths.logs_dir.exists(),
            "runtime_dir_writable": self.paths.runtime_dir.exists(),
            "state_file_present": self.paths.state_file.exists(),
            "log_file_present": self.paths.log_file.exists(),
        }
        ok = all(v for v in checks.values())
        return (
            EXIT_OK if ok else EXIT_ERROR,
            {
                "healthy": ok,
                "checks": checks,
                "log_file": str(self.paths.log_file),
                "status": status_payload if status_code == EXIT_OK else {},
                "agent_package_version": _current_version("mira"),
            },
        )

    def export_diagnostics(self) -> tuple[int, str]:
        self.paths.ensure()
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
        diagnostics_dir = self.paths.runtime_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        bundle = diagnostics_dir / f"diagnostics-{_now_iso().replace(':', '-')}.zip"

        _, doctor_payload = self.doctor()
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doctor.json", json.dumps(doctor_payload, ensure_ascii=False, indent=2) + "\n")
            if self.paths.state_file.exists():
                zf.write(self.paths.state_file, arcname="agent-service-state.json")
            if self.paths.log_file.exists():
                lines = self.paths.log_file.read_text(encoding="utf-8").splitlines()
                tail = "\n".join(lines[-DIAGNOSTICS_LOG_TAIL_LINES:]) + ("\n" if lines else "")
                zf.writestr("agent-service.log.tail", tail)
        self._append_log("diagnostics_exported", bundle=str(bundle))
        return EXIT_OK, str(bundle)


class SystemdUserServiceManager(LocalServiceManager):
    """Linux systemd --user manager."""

    SERVICE_MODE = "systemd-user"

    def _run_systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_unit(self, host: str, port: int) -> None:
        self.paths.systemd_unit.parent.mkdir(parents=True, exist_ok=True)
        exec_start = _gateway_service_command(host, port)
        content = f"""[Unit]
Description=Mira Local Agent Service
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:{self.paths.log_file}
StandardError=append:{self.paths.log_file}

[Install]
WantedBy=default.target
"""
        self.paths.systemd_unit.write_text(content, encoding="utf-8")

    def install_service(
        self,
        host: str | None = None,
        port: int | None = None,
        home: str | None = None,
        config_path: str | None = None,
    ) -> tuple[int, str]:
        code, msg = super().install_service(host, port, home, config_path)
        if code != EXIT_OK:
            return code, msg
        state = self.load_state()
        self._write_unit(
            str(state.get("host", "127.0.0.1")),
            int(state.get("port", DEFAULT_PORT))
        )
        self._run_systemctl("daemon-reload")
        enable = self._run_systemctl("enable", SYSTEMD_UNIT_NAME)
        if enable.returncode != 0:
            return EXIT_ERROR, enable.stderr.strip() or "failed to enable systemd user service"
        state["service_mode"] = "systemd-user"
        self.save_state(state)
        self._append_log("systemd_install_service", unit=str(self.paths.systemd_unit))
        return EXIT_OK, f"systemd user service installed ({self.paths.systemd_unit})"

    def uninstall_service(self) -> tuple[int, str]:
        self._run_systemctl("disable", "--now", SYSTEMD_UNIT_NAME)
        try:
            self.paths.systemd_unit.unlink(missing_ok=True)
        except OSError as exc:
            return EXIT_ERROR, f"failed to remove unit file: {exc}"
        self._run_systemctl("daemon-reload")
        code, msg = super().uninstall_service()
        self._append_log("systemd_uninstall_service", unit=str(self.paths.systemd_unit))
        return code, msg

    def start(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_systemctl("start", SYSTEMD_UNIT_NAME)
        if result.returncode != 0:
            self._append_log("systemd_start_failed", error=result.stderr.strip())
            return EXIT_ERROR, result.stderr.strip() or "failed to start systemd user service"
        state["running"] = True
        state["last_started_at"] = _now_iso()
        self.save_state(state)
        self._append_log("systemd_start_service", running=True)
        return EXIT_OK, "systemd user service started"

    def stop(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_systemctl("stop", SYSTEMD_UNIT_NAME)
        if result.returncode != 0:
            self._append_log("systemd_stop_failed", error=result.stderr.strip())
            return EXIT_ERROR, result.stderr.strip() or "failed to stop systemd user service"
        state["running"] = False
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
        self._append_log("systemd_stop_service", running=False)
        return EXIT_OK, "systemd user service stopped"

    def status(self) -> tuple[int, dict[str, Any]]:
        base_code, payload = super().status()
        result = self._run_systemctl("is-active", SYSTEMD_UNIT_NAME)
        payload["running"] = result.returncode == 0 and result.stdout.strip() == "active"
        payload["service_mode"] = "systemd-user"
        payload["systemd_unit"] = str(self.paths.systemd_unit)
        if result.returncode != 0 and payload.get("installed"):
            payload["last_systemd_error"] = result.stderr.strip()
        return base_code, payload


class WindowsBackgroundProcessManager(LocalServiceManager):
    """Legacy Windows detached background-process manager."""

    SERVICE_MODE = "windows-background"

    def _run_windows_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            check=False,
        )

    def _is_pid_running(self, pid: int | None) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        result = self._run_windows_tool("tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH")
        if result.returncode != 0:
            return False
        output = result.stdout.strip()
        return bool(output) and "No tasks are running" not in output

    def _terminate_pid(self, pid: int) -> subprocess.CompletedProcess[str]:
        return self._run_windows_tool("taskkill", "/PID", str(pid), "/T", "/F")

    def install_service(
        self,
        host: str | None = None,
        port: int | None = None,
        home: str | None = None,
        config_path: str | None = None,
    ) -> tuple[int, str]:
        code, msg = super().install_service(host, port, home, config_path)
        if code != EXIT_OK:
            return code, msg
        state = self.load_state()
        state["service_mode"] = self.SERVICE_MODE
        state["pid"] = None
        self.save_state(state)
        self._append_log("windows_install_service", mode="background-process")
        return EXIT_OK, "Windows background service metadata installed"

    def uninstall_service(self) -> tuple[int, str]:
        state = self.load_state()
        pid = state.get("pid")
        if isinstance(pid, int) and pid > 0 and self._is_pid_running(pid):
            self._terminate_pid(pid)
        code, msg = super().uninstall_service()
        state = self.load_state()
        state["pid"] = None
        state["service_mode"] = self.SERVICE_MODE
        self.save_state(state)
        self._append_log("windows_uninstall_service", service=WINDOWS_SERVICE_NAME)
        return code, msg

    def start(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"

        existing_pid = state.get("pid")
        if isinstance(existing_pid, int) and self._is_pid_running(existing_pid):
            state["running"] = True
            state["service_mode"] = self.SERVICE_MODE
            self.save_state(state)
            return EXIT_OK, "Windows background gateway already running"

        host = str(state.get("host", "127.0.0.1"))
        port = int(state.get("port", DEFAULT_PORT))
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        log_fp = self.paths.log_file.open("a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                _gateway_service_args(host, port),
                stdout=log_fp,
                stderr=log_fp,
                stdin=subprocess.DEVNULL,
                env=_independent_subprocess_env(PYTHONUNBUFFERED="1"),
                close_fds=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            log_fp.close()
            self._append_log("windows_start_failed", error=str(exc))
            return EXIT_ERROR, str(exc)
        finally:
            log_fp.close()

        time.sleep(0.3)
        if proc.poll() is not None:
            error = ""
            if self.paths.log_file.exists():
                lines = self.paths.log_file.read_text(encoding="utf-8").splitlines()
                error = lines[-1] if lines else ""
            self._append_log("windows_start_failed", error=error)
            return EXIT_ERROR, error or "failed to launch Windows background gateway"

        state["running"] = True
        state["pid"] = proc.pid
        state["service_mode"] = self.SERVICE_MODE
        state["last_started_at"] = _now_iso()
        self.save_state(state)
        self._append_log("windows_start_service", running=True, pid=proc.pid)
        return EXIT_OK, "Windows background gateway started"

    def stop(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"

        pid = state.get("pid")
        if isinstance(pid, int) and pid > 0 and self._is_pid_running(pid):
            result = self._terminate_pid(pid)
            if result.returncode != 0 and self._is_pid_running(pid):
                self._append_log("windows_stop_failed", error=result.stderr.strip())
                return EXIT_ERROR, result.stderr.strip() or "failed to stop Windows background gateway"

        state["running"] = False
        state["pid"] = None
        state["service_mode"] = self.SERVICE_MODE
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
        self._append_log("windows_stop_service", running=False)
        return EXIT_OK, "Windows background gateway stopped"

    def status(self) -> tuple[int, dict[str, Any]]:
        base_code, payload = super().status()
        state = self.load_state()
        pid = state.get("pid")
        running = self._is_pid_running(pid if isinstance(pid, int) else None)
        if state.get("running") != running or (not running and pid):
            state["running"] = running
            if not running:
                state["pid"] = None
            self.save_state(state)
            pid = state.get("pid")
        payload["running"] = running
        payload["service_mode"] = state.get("service_mode", self.SERVICE_MODE)
        payload["windows_service"] = WINDOWS_SERVICE_NAME
        payload["windows_pid"] = pid
        return base_code, payload


class WindowsServiceManager(LocalServiceManager):
    """Windows Service manager backed by a bundled WinSW service wrapper."""

    SERVICE_MODE = "windows-service"

    def __init__(self, paths: AgentPaths) -> None:
        super().__init__(paths)
        self._fallback = WindowsBackgroundProcessManager(paths)

    def _background_fallback_enabled(self) -> bool:
        value = os.environ.get("MIRA_ENGINE_WINDOWS_BACKGROUND_FALLBACK", "0").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _run_windows_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if platform.system().lower() == "windows":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(list(args), **kwargs)

    def _wrapper_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        env_path = os.environ.get("MIRA_ENGINE_SERVICE_WRAPPER", "").strip()
        if env_path:
            candidates.append(Path(env_path).expanduser())
        candidates.append(Path(sys.executable).resolve().with_name(WINDOWS_SERVICE_WRAPPER_NAME))
        meipass = getattr(sys, "_MEIPASS", None)
        if isinstance(meipass, str) and meipass:
            candidates.append(Path(meipass) / WINDOWS_SERVICE_WRAPPER_NAME)
        candidates.append(self.paths.runtime_dir / WINDOWS_SERVICE_WRAPPER_NAME)
        seen: set[Path] = set()
        unique: list[Path] = []
        for candidate in candidates:
            normalized = candidate.expanduser()
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return unique

    def _resolve_wrapper_source(self) -> Path | None:
        for candidate in self._wrapper_candidates():
            if candidate.is_file():
                return candidate
        return None

    def _staged_wrapper_path(self) -> Path:
        return self.paths.runtime_dir / WINDOWS_SERVICE_WRAPPER_NAME

    def _service_xml_path(self, wrapper_path: Path) -> Path:
        return wrapper_path.with_name(WINDOWS_SERVICE_CONFIG_NAME)

    def _release_staged_wrapper_for_update(self, source: Path) -> None:
        target = self._staged_wrapper_path()
        if source.resolve(strict=False) == target.resolve(strict=False):
            return
        if not target.is_file():
            return
        self._append_log("windows_service_prepare_wrapper_update", wrapper=str(target))
        self._run_wrapper("stop")
        self._run_wrapper("uninstall")

    def _stage_wrapper(self, source: Path) -> Path:
        self.paths.ensure()
        target = self._staged_wrapper_path()
        if source.resolve(strict=False) != target.resolve(strict=False):
            for attempt in range(1, 4):
                try:
                    shutil.copy2(source, target)
                    break
                except PermissionError:
                    if attempt >= 3:
                        raise
                    self._release_staged_wrapper_for_update(source)
                    time.sleep(0.5)
        return target

    def _write_service_xml(
        self,
        wrapper_path: Path,
        *,
        host: str,
        port: int,
        home: str | None,
        config_path: str | None,
    ) -> tuple[Path, Path]:
        from mira_engine.config.loader import get_home_dir

        home_path = Path(home).expanduser() if home else Path.home()
        config_file = (
            Path(config_path).expanduser()
            if config_path
            else (home_path / ".mira" / "config.json" if home else get_home_dir() / "config.json")
        )
        command = _gateway_service_args(host, port)
        executable = Path(command[0]).expanduser()
        arguments = subprocess.list2cmdline(command[1:])
        log_dir = self.paths.log_file.parent
        log_dir.mkdir(parents=True, exist_ok=True)

        def esc(value: object) -> str:
            return xml_escape(str(value), {'"': "&quot;"})

        payload = f"""<service>
  <id>{esc(WINDOWS_SERVICE_NAME)}</id>
  <name>{esc(WINDOWS_SERVICE_DISPLAY_NAME)}</name>
  <description>Mira local engine gateway for the desktop bundle.</description>
  <executable>{esc(executable)}</executable>
  <arguments>{esc(arguments)}</arguments>
  <workingdirectory>{esc(executable.parent)}</workingdirectory>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="5 sec" />
  <resetfailure>1 hour</resetfailure>
  <env name="PYTHONUNBUFFERED" value="1" />
  <env name="PYINSTALLER_RESET_ENVIRONMENT" value="1" />
  <env name="HOME" value="{esc(home_path)}" />
  <env name="USERPROFILE" value="{esc(home_path)}" />
  <env name="MIRA_CONFIG_PATH" value="{esc(config_file)}" />
  <logpath>{esc(log_dir)}</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>{LOG_ROTATE_BYTES}</sizeThreshold>
    <keepFiles>{LOG_ROTATE_FILES}</keepFiles>
  </log>
</service>
"""
        xml_path = self._service_xml_path(wrapper_path)
        xml_path.write_text(payload, encoding="utf-8")
        return home_path, config_file

    def _run_wrapper(self, command: str) -> subprocess.CompletedProcess[str]:
        wrapper = self._staged_wrapper_path()
        return self._run_windows_tool(str(wrapper), command)

    def _wrapper_status(self) -> tuple[bool, bool, str]:
        wrapper = self._staged_wrapper_path()
        if not wrapper.is_file():
            return False, False, "service wrapper is not staged"
        result = self._run_wrapper("status")
        output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        normalized = output.lower()
        installed = result.returncode == 0 and not any(
            marker in normalized
            for marker in ("nonexistent", "not installed", "does not exist")
        )
        running = any(marker in normalized for marker in ("started", "running"))
        return installed, running, output

    def _stop_legacy_background_if_needed(self) -> None:
        state = self.load_state()
        if state.get("service_mode") == WindowsBackgroundProcessManager.SERVICE_MODE:
            self._fallback.stop()

    def _fallback_install(
        self,
        *,
        host: str | None,
        port: int | None,
        home: str | None,
        config_path: str | None,
        reason: str,
    ) -> tuple[int, str]:
        if not self._background_fallback_enabled():
            return (
                EXIT_ERROR,
                f"{reason}; Windows background fallback is disabled because "
                "Mira requires a real Windows service. Approve the administrator "
                "prompt and retry.",
            )
        code, message = self._fallback.install_service(host, port, home, config_path)
        state = self.load_state()
        state["fallback_reason"] = reason
        self.save_state(state)
        self._append_log("windows_service_fallback_to_background", reason=reason)
        return code, f"{message} (fallback: {reason})"

    def install_service(
        self,
        host: str | None = None,
        port: int | None = None,
        home: str | None = None,
        config_path: str | None = None,
    ) -> tuple[int, str]:
        service_host = host or "127.0.0.1"
        service_port = port or DEFAULT_PORT
        source = self._resolve_wrapper_source()
        if source is None:
            return self._fallback_install(
                host=host,
                port=port,
                home=home,
                config_path=config_path,
                reason=f"{WINDOWS_SERVICE_WRAPPER_NAME} not found",
            )

        self._stop_legacy_background_if_needed()
        self._release_staged_wrapper_for_update(source)
        try:
            wrapper_path = self._stage_wrapper(source)
        except OSError as exc:
            message = (
                f"failed to stage {WINDOWS_SERVICE_WRAPPER_NAME}; "
                "the existing service wrapper may still be locked. "
                "Stop the Mira Engine service or restart Windows, then retry. "
                f"{exc}"
            )
            self._append_log(
                "windows_service_stage_wrapper_failed",
                source=str(source),
                target=str(self._staged_wrapper_path()),
                error=str(exc),
            )
            return EXIT_ERROR, message
        home_path, config_file = self._write_service_xml(
            wrapper_path,
            host=service_host,
            port=service_port,
            home=home,
            config_path=config_path,
        )

        self._run_wrapper("stop")
        self._run_wrapper("uninstall")
        install = self._run_wrapper("install")
        if install.returncode != 0:
            message = install.stderr.strip() or install.stdout.strip() or "failed to install Windows service"
            return self._fallback_install(
                host=host,
                port=port,
                home=home,
                config_path=config_path,
                reason=message,
            )

        start = self._run_wrapper("start")
        service_started = start.returncode == 0
        if not service_started:
            message = start.stderr.strip() or start.stdout.strip() or "failed to start Windows service"
            self._append_log("windows_service_start_after_install_failed", error=message)

        code, _ = super().install_service(
            service_host,
            service_port,
            str(home_path),
            str(config_file),
        )
        state = self.load_state()
        state["service_mode"] = self.SERVICE_MODE
        state["windows_service"] = WINDOWS_SERVICE_NAME
        state["windows_service_wrapper"] = str(wrapper_path)
        state["windows_service_config"] = str(self._service_xml_path(wrapper_path))
        state["engine_executable"] = _gateway_service_args(service_host, service_port)[0]
        state["running"] = service_started
        state["pid"] = None
        self.save_state(state)
        self._append_log(
            "windows_service_install",
            wrapper=str(wrapper_path),
            config=str(self._service_xml_path(wrapper_path)),
            home=str(home_path),
            config_path=str(config_file),
            running=service_started,
        )
        if service_started:
            return code, f"Windows service installed and started ({WINDOWS_SERVICE_NAME})"
        return code, f"Windows service installed ({WINDOWS_SERVICE_NAME})"

    def uninstall_service(self) -> tuple[int, str]:
        state = self.load_state()
        if state.get("service_mode") == WindowsBackgroundProcessManager.SERVICE_MODE:
            return self._fallback.uninstall_service()
        self._run_wrapper("stop")
        result = self._run_wrapper("uninstall")
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "failed to uninstall Windows service"
            self._append_log("windows_service_uninstall_failed", error=message)
            return EXIT_ERROR, message
        code, msg = super().uninstall_service()
        state = self.load_state()
        state["service_mode"] = self.SERVICE_MODE
        state["pid"] = None
        state["windows_service"] = WINDOWS_SERVICE_NAME
        self.save_state(state)
        self._append_log("windows_service_uninstall", service=WINDOWS_SERVICE_NAME)
        return code, msg

    def start(self) -> tuple[int, str]:
        state = self.load_state()
        if state.get("service_mode") == WindowsBackgroundProcessManager.SERVICE_MODE:
            return self._fallback.start()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_wrapper("start")
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "failed to start Windows service"
            self._append_log("windows_service_start_failed", error=message)
            return EXIT_ERROR, message
        state["running"] = True
        state["service_mode"] = self.SERVICE_MODE
        state["last_started_at"] = _now_iso()
        self.save_state(state)
        self._append_log("windows_service_start", running=True)
        return EXIT_OK, "Windows service started"

    def stop(self) -> tuple[int, str]:
        state = self.load_state()
        if state.get("service_mode") == WindowsBackgroundProcessManager.SERVICE_MODE:
            return self._fallback.stop()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_wrapper("stop")
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "failed to stop Windows service"
            self._append_log("windows_service_stop_failed", error=message)
            return EXIT_ERROR, message
        state["running"] = False
        state["service_mode"] = self.SERVICE_MODE
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
        self._append_log("windows_service_stop", running=False)
        return EXIT_OK, "Windows service stopped"

    def status(self) -> tuple[int, dict[str, Any]]:
        state = self.load_state()
        if state.get("service_mode") == WindowsBackgroundProcessManager.SERVICE_MODE:
            return self._fallback.status()
        base_code, payload = super().status()
        installed, running, status_output = self._wrapper_status()
        if payload.get("installed") != installed or payload.get("running") != running:
            state["installed"] = installed
            state["running"] = running
            self.save_state(state)
        payload["installed"] = installed
        payload["running"] = running
        payload["service_mode"] = self.SERVICE_MODE
        payload["windows_service"] = WINDOWS_SERVICE_NAME
        payload["windows_service_wrapper"] = str(self._staged_wrapper_path())
        payload["windows_service_config"] = str(self._service_xml_path(self._staged_wrapper_path()))
        if status_output:
            payload["windows_service_status"] = status_output
        return base_code, payload

    def doctor(self) -> tuple[int, dict[str, Any]]:
        code, payload = super().doctor()
        checks = payload.get("checks", {})
        if isinstance(checks, dict):
            installed, running, status_output = self._wrapper_status()
            checks["windows_service_wrapper_present"] = self._staged_wrapper_path().is_file()
            checks["windows_service_config_present"] = self._service_xml_path(self._staged_wrapper_path()).is_file()
            checks["windows_service_installed"] = installed
            checks["windows_service_running"] = running
            payload["checks"] = checks
            payload["healthy"] = all(bool(v) for v in checks.values())
            payload["windows_service_status"] = status_output
        payload["windows_service"] = WINDOWS_SERVICE_NAME
        payload["windows_service_wrapper"] = str(self._staged_wrapper_path())
        return (EXIT_OK if payload.get("healthy") else EXIT_ERROR), payload


class LaunchdServiceManager(LocalServiceManager):
    """macOS launchd-backed lifecycle manager."""

    SERVICE_MODE = "launchd"

    @property
    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def _service_target(self) -> str:
        return f"{self._domain}/{LAUNCHD_LABEL}"

    def _run_launchctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_plist(
        self,
        host: str,
        port: int,
        *,
        home: str | None,
        config_path: str | None,
    ) -> None:
        self.paths.launchd_plist.parent.mkdir(parents=True, exist_ok=True)
        home_path = Path(home).expanduser() if home else self.paths.root.parent
        config_file = (
            Path(config_path).expanduser()
            if config_path
            else (home_path / ".mira" / "config.json" if home else self.paths.root / "config.json")
        )
        payload = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": _gateway_service_args(host, port),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(self.paths.log_file),
            "StandardErrorPath": str(self.paths.log_file),
            "EnvironmentVariables": {
                "HOME": str(home_path),
                "MIRA_CONFIG_PATH": str(config_file),
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
                "PYTHONUNBUFFERED": "1",
            },
        }
        with self.paths.launchd_plist.open("wb") as fp:
            plistlib.dump(payload, fp)

    def _wait_for_service_unloaded(self, timeout_s: float = 15.0) -> bool:
        """Poll until the LaunchAgent is no longer registered in our domain.

        ``launchctl bootout`` returns as soon as it has signalled the service;
        the underlying process can take several seconds to actually exit
        (especially when it has active aiohttp / WebSocket clients to drain).
        If we ``bootstrap`` the replacement plist before launchd has fully
        torn down the previous instance we get the opaque
        ``Bootstrap failed: 5: Input/output error``. Polling ``launchctl
        print`` lets us wait for the label to leave the domain before
        attempting to bootstrap again.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self._run_launchctl("print", self._service_target)
            # Non-zero return means launchd no longer has the label in this
            # domain — exactly the precondition `bootstrap` needs.
            if result.returncode != 0:
                return True
            time.sleep(0.25)
        return False

    def _teardown_existing_job(self) -> None:
        """Best-effort cleanup of any already-loaded LaunchAgent with our label.

        `launchctl bootstrap` returns the opaque "Bootstrap failed: 5: Input/
        output error" whenever the label is already registered in the target
        domain. Booting it out, removing the cached label, and then waiting
        until the label has actually left the domain makes the install
        idempotent even when the previous engine is busy draining clients.
        """
        self._run_launchctl("bootout", self._service_target)
        self._run_launchctl("remove", LAUNCHD_LABEL)
        self._wait_for_service_unloaded()

    def install_service(
        self,
        host: str | None = None,
        port: int | None = None,
        home: str | None = None,
        config_path: str | None = None,
    ) -> tuple[int, str]:
        previous_state = self.load_state()
        self.paths.ensure()
        service_host = str(host if host is not None else previous_state.get("host", "127.0.0.1"))
        service_port = int(port if port is not None else previous_state.get("port", DEFAULT_PORT))
        service_home = (
            previous_state.get("home") if isinstance(previous_state.get("home"), str) else home
        )
        service_config_path = (
            previous_state.get("config_path")
            if isinstance(previous_state.get("config_path"), str)
            else config_path
        )

        # Remember whether a plist already exists so we can restore it if the
        # bootstrap below fails (rollback for the transactional install).
        plist_path = self.paths.launchd_plist
        previous_plist: bytes | None = None
        if plist_path.is_file():
            try:
                previous_plist = plist_path.read_bytes()
            except OSError:
                previous_plist = None

        self._teardown_existing_job()
        self._write_plist(
            service_host,
            service_port,
            home=service_home,
            config_path=service_config_path,
        )
        bootstrap = self._run_launchctl("bootstrap", self._domain, str(plist_path))
        # Retry once after another cleanup pass — launchctl occasionally races
        # with its own shutdown when the previous job exits during bootout.
        if bootstrap.returncode != 0:
            self._teardown_existing_job()
            bootstrap = self._run_launchctl("bootstrap", self._domain, str(plist_path))
            if bootstrap.returncode != 0:
                # Roll back the plist so a partial install does not leave
                # disk state pointing at an executable that never loaded.
                if previous_plist is not None:
                    try:
                        plist_path.write_bytes(previous_plist)
                    except OSError:
                        pass
                else:
                    try:
                        plist_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                return EXIT_ERROR, bootstrap.stderr.strip() or "failed to bootstrap launchd service"

        # Bootstrap succeeded — persist the new identity. If the base class
        # state write somehow fails, undo the launchd job so the on-disk
        # state stays consistent with what is actually running.
        code, msg = super().install_service(host, port, home, config_path)
        if code != EXIT_OK:
            self._teardown_existing_job()
            return code, msg
        state = self.load_state()
        state["service_mode"] = "launchd"
        self.save_state(state)
        self._append_log("launchd_install_service", plist=str(plist_path))
        return EXIT_OK, f"launchd service installed ({plist_path})"

    def uninstall_service(self) -> tuple[int, str]:
        # Symmetric with install_service: bootout + remove drops the label
        # from launchd's cache so a subsequent reinstall starts from a
        # clean slate.
        self._teardown_existing_job()
        try:
            self.paths.launchd_plist.unlink(missing_ok=True)
        except OSError as exc:
            return EXIT_ERROR, f"failed to remove plist: {exc}"
        code, msg = super().uninstall_service()
        self._append_log("launchd_uninstall_service", plist=str(self.paths.launchd_plist))
        return code, msg

    def start(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_launchctl("kickstart", "-k", self._service_target)
        if result.returncode != 0:
            self._append_log("launchd_start_failed", error=result.stderr.strip())
            return EXIT_ERROR, result.stderr.strip() or "failed to start launchd service"
        state["running"] = True
        state["last_started_at"] = _now_iso()
        self.save_state(state)
        self._append_log("launchd_start_service", running=True)
        return EXIT_OK, "launchd service started"

    def stop(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_launchctl("stop", LAUNCHD_LABEL)
        if result.returncode != 0:
            self._append_log("launchd_stop_failed", error=result.stderr.strip())
            return EXIT_ERROR, result.stderr.strip() or "failed to stop launchd service"
        state["running"] = False
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
        self._append_log("launchd_stop_service", running=False)
        return EXIT_OK, "launchd service stopped"

    def status(self) -> tuple[int, dict[str, Any]]:
        base_code, payload = super().status()
        result = self._run_launchctl("print", self._service_target)
        payload["running"] = result.returncode == 0
        payload["service_mode"] = "launchd"
        payload["launchd_label"] = LAUNCHD_LABEL
        payload["launchd_plist"] = str(self.paths.launchd_plist)
        try:
            with self.paths.launchd_plist.open("rb") as fp:
                plist = plistlib.load(fp)
            args = plist.get("ProgramArguments") if isinstance(plist, dict) else None
            if isinstance(args, list) and args:
                payload["launchd_program"] = args[0]
        except (OSError, plistlib.InvalidFileException):
            pass
        if result.returncode != 0 and payload.get("installed"):
            payload["last_launchctl_error"] = result.stderr.strip()
        return base_code, payload

    def doctor(self) -> tuple[int, dict[str, Any]]:
        code, payload = super().doctor()
        checks = payload.get("checks", {})
        if isinstance(checks, dict):
            checks["launchd_plist_present"] = self.paths.launchd_plist.exists()
            launchctl = self._run_launchctl("print", self._service_target)
            checks["launchctl_query_ok"] = launchctl.returncode in {0, 113}
            payload["checks"] = checks
            payload["healthy"] = all(bool(v) for v in checks.values())
        payload["launchd_plist"] = str(self.paths.launchd_plist)
        return (EXIT_OK if payload.get("healthy") else EXIT_ERROR), payload


def _manager(paths: AgentPaths | None = None) -> LocalServiceManager:
    mode = os.environ.get("MIRA_AGENT_SERVICE_MODE", "auto").strip().lower()
    paths = paths or AgentPaths.default()
    if mode == "launchd":
        return LaunchdServiceManager(paths)
    if mode == "systemd":
        return SystemdUserServiceManager(paths)
    if mode == "windows":
        return WindowsServiceManager(paths)
    if mode == "windows-background":
        return WindowsBackgroundProcessManager(paths)
    if mode == "local":
        return LocalServiceManager(paths)
    platform_name = platform.system().lower()
    if platform_name == "darwin":
        return LaunchdServiceManager(paths)
    if platform_name == "linux":
        return SystemdUserServiceManager(paths)
    if platform_name == "windows":
        return WindowsServiceManager(paths)
    return LocalServiceManager(paths)


def _manager_for_home(home: str | None = None) -> LocalServiceManager:
    paths = AgentPaths.for_home(Path(home)) if home else None
    return _manager(paths)


def _current_version(package: str) -> str | None:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return None


def _gateway_service_args(host: str, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "run-gateway",
            "--host",
            host,
            "--port",
            str(port),
        ]
    return [
        sys.executable,
        "-m",
        "mira_engine.cli.commands",
        "gateway",
        "--host",
        host,
        "--port",
        str(port),
    ]


def _independent_subprocess_env(**extra: str) -> dict[str, str]:
    env = {**os.environ, **extra}
    if getattr(sys, "frozen", False):
        # Give long-lived children their own PyInstaller onefile extraction dir.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _gateway_service_command(host: str, port: int) -> str:
    args = _gateway_service_args(host, port)
    if platform.system().lower() == "windows":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _pip_upgrade(package_spec: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", package_spec],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return EXIT_OK, result.stdout.strip() or f"upgraded {package_spec}"
    return EXIT_ERROR, result.stderr.strip() or f"failed to upgrade {package_spec}"


def _health_check(port: int, timeout_s: float = 3.0) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


@app.command("install-service")
def install_service(
    host: str = typer.Option("127.0.0.1", "--host", help="Gateway host"),
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Gateway port"),
    home: str | None = typer.Option(
        None,
        "--home",
        help="User home directory for Windows service environment.",
    ),
    config_path: str | None = typer.Option(
        None,
        "--config",
        help="Config path for Windows service environment.",
    ),
) -> None:
    code, message = _manager_for_home(home).install_service(
        host=host,
        port=port,
        home=home,
        config_path=config_path,
    )
    console.print(message)
    raise typer.Exit(code)


@app.command()
def uninstall_service(
    home: str | None = typer.Option(None, "--home", help="User home directory for service state."),
) -> None:
    code, message = _manager_for_home(home).uninstall_service()
    console.print(message)
    raise typer.Exit(code)


@app.command()
def start(
    home: str | None = typer.Option(None, "--home", help="User home directory for service state."),
) -> None:
    code, message = _manager_for_home(home).start()
    console.print(message)
    raise typer.Exit(code)


@app.command()
def stop(
    home: str | None = typer.Option(None, "--home", help="User home directory for service state."),
) -> None:
    code, message = _manager_for_home(home).stop()
    console.print(message)
    raise typer.Exit(code)


@app.command()
def status(
    home: str | None = typer.Option(None, "--home", help="User home directory for service state."),
) -> None:
    code, payload = _manager_for_home(home).status()
    console.print_json(data=payload)
    raise typer.Exit(code)


@app.command()
def logs(
    home: str | None = typer.Option(None, "--home", help="User home directory for service state."),
) -> None:
    path = _manager_for_home(home).paths.log_file
    console.print(str(path))
    raise typer.Exit(EXIT_OK)


@app.command()
def doctor(
    export: bool = typer.Option(False, "--export", help="Export diagnostics bundle."),
    home: str | None = typer.Option(None, "--home", help="User home directory for service state."),
) -> None:
    manager = _manager_for_home(home)
    code, payload = manager.doctor()
    if export:
        export_code, bundle_path = manager.export_diagnostics()
        payload["diagnostics_bundle"] = bundle_path
        if export_code != EXIT_OK:
            code = EXIT_ERROR
    console.print_json(data=payload)
    raise typer.Exit(code)


@app.command()
def upgrade(
    package: str = typer.Option("mira-engine", "--package", help="Package name to upgrade."),
) -> None:
    manager = _manager()
    status_code, status_payload = manager.status()
    if status_code != EXIT_OK:
        console.print("Unable to inspect current service status.")
        raise typer.Exit(EXIT_ERROR)

    installed = bool(status_payload.get("installed"))
    if not installed:
        console.print("Service is not installed. Run `mira-engine install-service` first.")
        raise typer.Exit(EXIT_NOT_INSTALLED)

    port = int(status_payload.get("port", DEFAULT_PORT))
    prev_version = _current_version(package)
    backup_file = manager.paths.backups_dir / f"upgrade-backup-{_now_iso().replace(':', '-')}.json"
    manager.paths.ensure()
    if manager.paths.state_file.exists():
        backup_file.write_text(manager.paths.state_file.read_text(encoding="utf-8"), encoding="utf-8")

    stop_code, stop_msg = manager.stop()
    if stop_code not in {EXIT_OK, EXIT_NOT_INSTALLED}:
        console.print(f"Failed to stop service before upgrade: {stop_msg}")
        raise typer.Exit(EXIT_ERROR)

    up_code, up_msg = _pip_upgrade(package)
    if up_code != EXIT_OK:
        console.print(f"Upgrade failed: {up_msg}")
        if prev_version:
            rollback_code, rollback_msg = _pip_upgrade(f"{package}=={prev_version}")
            if rollback_code != EXIT_OK:
                console.print(f"Rollback package install failed: {rollback_msg}")
                raise typer.Exit(EXIT_ERROR)
            console.print(f"Rolled back package to {package}=={prev_version}")
        manager.start()
        raise typer.Exit(EXIT_ERROR)

    start_code, start_msg = manager.start()
    if start_code != EXIT_OK:
        console.print(f"Upgrade applied but service failed to start: {start_msg}")
        if prev_version:
            _pip_upgrade(f"{package}=={prev_version}")
            manager.start()
        raise typer.Exit(EXIT_ERROR)

    if not _health_check(port):
        console.print("Service started but health check failed; attempting rollback.")
        manager.stop()
        if prev_version:
            _pip_upgrade(f"{package}=={prev_version}")
            manager.start()
        raise typer.Exit(EXIT_ERROR)

    new_version = _current_version(package)
    console.print(f"Upgrade successful: {prev_version or 'unknown'} -> {new_version or 'unknown'}")
    raise typer.Exit(EXIT_OK)


@app.command("run-gateway", hidden=True)
def run_gateway(
    host: str = typer.Option("127.0.0.1", "--host", help="Gateway host"),
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Gateway port"),
) -> None:
    from mira_engine.cli.commands import gateway as gateway_cmd

    gateway_cmd(
        host=host,
        port=port,
        workspace=None,
        verbose=False,
        config=None,
        no_ui=False,
    )


if __name__ == "__main__":
    app()
