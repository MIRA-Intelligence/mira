"""CLI entrypoint for local MedPilot engine service lifecycle."""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
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
        )

    def ensure(self) -> None:
        for path in (self.config_dir, self.data_dir, self.logs_dir, self.runtime_dir):
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
            "port": 46321,
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


def _manager() -> LocalServiceManager:
    return LocalServiceManager(AgentPaths.default())


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


if __name__ == "__main__":
    app()
