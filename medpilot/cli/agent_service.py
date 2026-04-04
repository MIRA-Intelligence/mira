"""CLI entrypoint for local MedPilot engine service lifecycle."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

app = typer.Typer(
    name="medpilot-agent",
    help="Manage local MedPilot engine service lifecycle.",
    no_args_is_help=True,
)
console = Console()

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_INSTALLED = 2
LAUNCHD_LABEL = "com.projectmedpilot.agent"
DEFAULT_PORT = 46321


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
    backups_dir: Path

    @classmethod
    def default(cls) -> "AgentPaths":
        root = Path.home() / ".medpilot"
        return cls(
            root=root,
            config_dir=root / "config",
            data_dir=root / "data",
            logs_dir=root / "logs",
            runtime_dir=root / "runtime",
            state_file=root / "runtime" / "agent-service-state.json",
            log_file=root / "logs" / "agent-service.log",
            launchd_plist=Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
            backups_dir=root / "runtime" / "backups",
        )

    def ensure(self) -> None:
        for path in (self.config_dir, self.data_dir, self.logs_dir, self.runtime_dir, self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.log_file.touch(exist_ok=True)


class LocalServiceManager:
    """File-backed lifecycle manager used as the portable default."""

    def __init__(self, paths: AgentPaths) -> None:
        self.paths = paths

    def _default_state(self) -> dict[str, Any]:
        return {
            "installed": False,
            "running": False,
            "service_mode": "local-skeleton",
            "platform": platform.system().lower(),
            "port": DEFAULT_PORT,
            "installed_at": None,
            "last_started_at": None,
            "last_stopped_at": None,
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

    def install_service(self) -> tuple[int, str]:
        state = self.load_state()
        self.paths.ensure()
        state["installed"] = True
        state["installed_at"] = state.get("installed_at") or _now_iso()
        self.save_state(state)
        return EXIT_OK, "service metadata installed"

    def uninstall_service(self) -> tuple[int, str]:
        state = self.load_state()
        state["installed"] = False
        state["running"] = False
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
        return EXIT_OK, "service metadata removed"

    def start(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        state["running"] = True
        state["last_started_at"] = _now_iso()
        self.save_state(state)
        return EXIT_OK, "service marked as running"

    def stop(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        state["running"] = False
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
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
        checks = {
            "python_executable": bool(sys.executable),
            "config_dir_writable": self.paths.config_dir.exists(),
            "data_dir_writable": self.paths.data_dir.exists(),
            "logs_dir_writable": self.paths.logs_dir.exists(),
            "runtime_dir_writable": self.paths.runtime_dir.exists(),
            "state_file_present": self.paths.state_file.exists(),
        }
        ok = all(v for v in checks.values())
        return (
            EXIT_OK if ok else EXIT_ERROR,
            {"healthy": ok, "checks": checks, "log_file": str(self.paths.log_file)},
        )


class LaunchdServiceManager(LocalServiceManager):
    """macOS launchd-backed lifecycle manager."""

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

    def _write_plist(self, port: int) -> None:
        self.paths.launchd_plist.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "medpilot.cli.commands",
                "gateway",
                "--port",
                str(port),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(self.paths.log_file),
            "StandardErrorPath": str(self.paths.log_file),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }
        with self.paths.launchd_plist.open("wb") as fp:
            plistlib.dump(payload, fp)

    def install_service(self) -> tuple[int, str]:
        code, msg = super().install_service()
        if code != EXIT_OK:
            return code, msg
        state = self.load_state()
        self._write_plist(int(state.get("port", DEFAULT_PORT)))
        bootstrap = self._run_launchctl("bootstrap", self._domain, str(self.paths.launchd_plist))
        # launchd returns non-zero when already loaded; try cleanup then retry once.
        if bootstrap.returncode != 0:
            self._run_launchctl("bootout", self._service_target)
            bootstrap = self._run_launchctl("bootstrap", self._domain, str(self.paths.launchd_plist))
            if bootstrap.returncode != 0:
                return EXIT_ERROR, bootstrap.stderr.strip() or "failed to bootstrap launchd service"
        state["service_mode"] = "launchd"
        self.save_state(state)
        return EXIT_OK, f"launchd service installed ({self.paths.launchd_plist})"

    def uninstall_service(self) -> tuple[int, str]:
        self._run_launchctl("bootout", self._service_target)
        try:
            self.paths.launchd_plist.unlink(missing_ok=True)
        except OSError as exc:
            return EXIT_ERROR, f"failed to remove plist: {exc}"
        return super().uninstall_service()

    def start(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_launchctl("kickstart", "-k", self._service_target)
        if result.returncode != 0:
            return EXIT_ERROR, result.stderr.strip() or "failed to start launchd service"
        state["running"] = True
        state["last_started_at"] = _now_iso()
        self.save_state(state)
        return EXIT_OK, "launchd service started"

    def stop(self) -> tuple[int, str]:
        state = self.load_state()
        if not state.get("installed"):
            return EXIT_NOT_INSTALLED, "service is not installed; run install-service first"
        result = self._run_launchctl("stop", LAUNCHD_LABEL)
        if result.returncode != 0:
            return EXIT_ERROR, result.stderr.strip() or "failed to stop launchd service"
        state["running"] = False
        state["last_stopped_at"] = _now_iso()
        self.save_state(state)
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
    mode = os.environ.get("MEDPILOT_AGENT_SERVICE_MODE", "auto").strip().lower()
    paths = AgentPaths.default()
    if mode == "launchd":
        return LaunchdServiceManager(paths)
    if mode == "local":
        return LocalServiceManager(paths)
    if platform.system().lower() == "darwin":
        return LaunchdServiceManager(paths)
    return LocalServiceManager(paths)


def _current_version(package: str) -> str | None:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return None


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
def install_service() -> None:
    code, message = _manager().install_service()
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
def doctor() -> None:
    code, payload = _manager().doctor()
    console.print_json(data=payload)
    raise typer.Exit(code)


@app.command()
def upgrade(
    package: str = typer.Option("medpilot-ai", "--package", help="Package name to upgrade."),
) -> None:
    manager = _manager()
    status_code, status_payload = manager.status()
    if status_code != EXIT_OK:
        console.print("Unable to inspect current service status.")
        raise typer.Exit(EXIT_ERROR)

    installed = bool(status_payload.get("installed"))
    if not installed:
        console.print("Service is not installed. Run `medpilot-agent install-service` first.")
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


if __name__ == "__main__":
    app()
