"""Verified lifecycle for explicitly selected, non-service gateway processes.

Never scan process names to kill them. Discovery is restricted to MIRA_HOME,
then PID birth time, command and launch mode are checked before signalling.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import psutil

from mira_engine.utils.locks import atomic_write_text

IS_WINDOWS = os.name == "nt"


def is_gateway_command(command: list[str]) -> bool:
    names = {Path(arg).name.lower() for arg in command[:3]}
    return (
        "gateway" in command and ("mira" in names or "mira.exe" in names
                                  or "mira_engine.cli.commands" in command[:3])
    ) or ("run-gateway" in command and bool(names & {"mira-engine", "mira-engine.exe"}))


def verified_process(record: dict, root: Path) -> psutil.Process | None:
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        command = proc.cmdline()
        if not is_gateway_command(command):
            raise RuntimeError(f"PID {pid} no longer belongs to a Mira gateway; refusing to signal it")
        created = record.get("create_time")
        if created is not None:
            if abs(proc.create_time() - float(created)) > 0.01:
                raise RuntimeError(f"PID {pid} has been reused; refusing to signal it")
        else:
            # Old discovery files have no birth time. Require the process's
            # own environment to identify its home before migrating the record.
            env = proc.environ()
            actual_root = Path(env.get("MIRA_HOME") or str(Path(env.get("HOME") or env.get("USERPROFILE", "")) / ".mira"))
            if actual_root.resolve() != root.resolve():
                raise RuntimeError(f"cannot verify the home of legacy gateway PID {pid}")
        return proc
    except psutil.NoSuchProcess:
        return None


def gateway_records(root: Path) -> list[dict]:
    path = root / "runtime" / "gateways.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("instances"), dict):
        raise RuntimeError(f"invalid gateway discovery file: {path}")
    result = []
    for record in data["instances"].values():
        if not isinstance(record, dict):
            raise RuntimeError("invalid gateway discovery entry")
        proc = verified_process(record, root)
        if proc is not None:
            result.append({**record, "create_time": proc.create_time()})
    return result


def gateway_action(manager, action: str, port: int | None = None, *, force: bool = False) -> tuple[int, str | dict]:
    from mira_engine.cli.agent_service import (
        _gateway_healthy,
        _process_tree,
        _tree_stopped,
        _wait_until,
    )

    root = manager.paths.root
    records = gateway_records(root)
    if port is not None:
        records = [item for item in records if item.get("port") == port]
    if action == "status":
        return 0, {"service_mode": "standalone", "mira_home": str(root), "instances": [
            {**r, "running": True, "healthy": _gateway_healthy(r["pid"], r["host"], r["port"], r.get("config_path"))}
            for r in records
        ], "running": bool(records)}
    if len(records) > 1:
        return 1, "multiple gateways found; select one with --port"
    snapshot_path = root / "runtime" / f"standalone-{port or 'default'}.json"
    record = records[0] if records else None
    if record and record.get("launch_mode", "standalone") != "standalone":
        return 1, "selected gateway is service-managed; omit --gateway and use service stop/restart"
    if record and action == "start":
        if _gateway_healthy(record["pid"], record["host"], record["port"], record.get("config_path")):
            return 0, "selected gateway already running and healthy"
        return 1, "selected gateway is running but unhealthy; use restart"
    if record:
        proc = verified_process(record, root)
        if proc is None:
            return 1, "gateway exited during inspection; retry"
        env = proc.environ()
        if env.get("MIRA_SERVICE_MODE") or ("run-gateway" in proc.cmdline() and "launch_mode" not in record):
            return 1, "gateway may be supervised; use its service manager"
        if "launch_mode" not in record:
            _, service = manager.status()
            if record["pid"] in {p.pid for p in _process_tree(service.get("pid"))}:
                return 1, "legacy gateway is service-managed; omit --gateway"
        snapshot = {**record, "command": proc.cmdline(), "cwd": proc.cwd(),
                    "config_path": record.get("config_path") or env.get("MIRA_CONFIG_PATH")}
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(snapshot_path, json.dumps(snapshot, ensure_ascii=False, indent=2))
        # psutil checks PID birth time before delivering a signal.
        if IS_WINDOWS and not force:
            # Detached Windows processes cannot receive CTRL_BREAK reliably.
            # Do not silently turn a normal stop into TerminateProcess.
            return 1, "standalone Windows gateway requires --force; prefer the Windows service for graceful stop"
        processes = [proc, *proc.children(recursive=True)]
        if force:
            for owned in reversed(processes):
                try:
                    owned.kill()
                except psutil.NoSuchProcess:
                    pass
        else:
            proc.send_signal(signal.SIGTERM)
        if not _wait_until(lambda: _tree_stopped(processes)):
            return 1, "gateway stop timed out; no replacement was started"
    if action == "stop":
        return 0, "selected gateway stopped" if record else "selected gateway already stopped"
    if not snapshot_path.is_file():
        return 2, "no saved gateway launch; start mira gateway once or install-service"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    command = snapshot["command"]
    if not is_gateway_command(command):
        return 1, "saved command is not a Mira gateway"
    env = {**os.environ, "MIRA_HOME": str(root), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    env.pop("MIRA_SERVICE_MODE", None)
    if snapshot.get("config_path"):
        env["MIRA_CONFIG_PATH"] = snapshot["config_path"]
    manager.paths.ensure()
    process_options = {}
    if IS_WINDOWS:
        process_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        )
    with manager.paths.log_file.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(command, cwd=snapshot["cwd"], env=env, stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=not IS_WINDOWS, **process_options)
    ready = _wait_until(lambda: proc.poll() is not None or _gateway_healthy(
        proc.pid, snapshot["host"], snapshot["port"], snapshot.get("config_path"),
    ))
    if not ready or proc.poll() is not None:
        return 1, f"gateway failed to become healthy; inspect {manager.paths.log_file}"
    return 0, "selected gateway started and healthy"
