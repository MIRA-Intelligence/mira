"""CLI entrypoint for local Mira engine service lifecycle."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shlex
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

import typer
from rich.console import Console

from mira_engine.utils.migration import run_startup_migrations

run_startup_migrations()

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
DEFAULT_PORT = 18790
LOG_ROTATE_BYTES = 1_000_000
LOG_ROTATE_FILES = 3
DIAGNOSTICS_LOG_TAIL_LINES = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        root = Path.home() / ".mira"
        return cls(
            root=root,
            config_dir=root / "config",
            data_dir=root / "data",
            logs_dir=root / "logs",
            runtime_dir=root / "runtime",
            state_file=root / "runtime" / "agent-service-state.json",
            log_file=root / "logs" / "agent-service.log",
            launchd_plist=Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
            systemd_unit=Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME,
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

    def install_service(self, host: str | None = None, port: int | None = None) -> tuple[int, str]:
        state = self.load_state()
        self.paths.ensure()
        state["installed"] = True
        state["installed_at"] = state.get("installed_at") or _now_iso()
        if host is not None:
            state["host"] = host
        if port is not None:
            state["port"] = port
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

    def install_service(self, host: str | None = None, port: int | None = None) -> tuple[int, str]:
        code, msg = super().install_service(host, port)
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


class WindowsServiceManager(LocalServiceManager):
    """Windows detached background-process manager."""

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

    def install_service(self, host: str | None = None, port: int | None = None) -> tuple[int, str]:
        code, msg = super().install_service(host, port)
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
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        log_fp = self.paths.log_file.open("a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                _gateway_service_args(host, port),
                stdout=log_fp,
                stderr=log_fp,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
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

    def _write_plist(self, host: str, port: int) -> None:
        self.paths.launchd_plist.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": _gateway_service_args(host, port),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(self.paths.log_file),
            "StandardErrorPath": str(self.paths.log_file),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }
        with self.paths.launchd_plist.open("wb") as fp:
            plistlib.dump(payload, fp)

    def install_service(self, host: str | None = None, port: int | None = None) -> tuple[int, str]:
        code, msg = super().install_service(host, port)
        if code != EXIT_OK:
            return code, msg
        state = self.load_state()
        self._write_plist(
            str(state.get("host", "127.0.0.1")),
            int(state.get("port", DEFAULT_PORT))
        )
        bootstrap = self._run_launchctl("bootstrap", self._domain, str(self.paths.launchd_plist))
        # launchd returns non-zero when already loaded; try cleanup then retry once.
        if bootstrap.returncode != 0:
            self._run_launchctl("bootout", self._service_target)
            bootstrap = self._run_launchctl("bootstrap", self._domain, str(self.paths.launchd_plist))
            if bootstrap.returncode != 0:
                return EXIT_ERROR, bootstrap.stderr.strip() or "failed to bootstrap launchd service"
        state["service_mode"] = "launchd"
        self.save_state(state)
        self._append_log("launchd_install_service", plist=str(self.paths.launchd_plist))
        return EXIT_OK, f"launchd service installed ({self.paths.launchd_plist})"

    def uninstall_service(self) -> tuple[int, str]:
        self._run_launchctl("bootout", self._service_target)
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


def _manager() -> LocalServiceManager:
    mode = os.environ.get("MIRA_AGENT_SERVICE_MODE", "auto").strip().lower()
    paths = AgentPaths.default()
    if mode == "launchd":
        return LaunchdServiceManager(paths)
    if mode == "systemd":
        return SystemdUserServiceManager(paths)
    if mode == "windows":
        return WindowsServiceManager(paths)
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
) -> None:
    code, message = _manager().install_service(host=host, port=port)
    console.print(message)
    raise typer.Exit(code)


@app.command()
def uninstall_service() -> None:
    code, message = _manager().uninstall_service()
    console.print(message)
    raise typer.Exit(code)


@app.command()
def start() -> None:
    code, message = _manager().start()
    console.print(message)
    raise typer.Exit(code)


@app.command()
def stop() -> None:
    code, message = _manager().stop()
    console.print(message)
    raise typer.Exit(code)


@app.command()
def status() -> None:
    code, payload = _manager().status()
    console.print_json(data=payload)
    raise typer.Exit(code)


@app.command()
def logs() -> None:
    path = _manager().paths.log_file
    console.print(str(path))
    raise typer.Exit(EXIT_OK)


@app.command()
def doctor(
    export: bool = typer.Option(False, "--export", help="Export diagnostics bundle."),
) -> None:
    manager = _manager()
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

    gateway_cmd(host=host, port=port, workspace=None, verbose=False, config=None)


if __name__ == "__main__":
    app()
