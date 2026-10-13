import asyncio
import json
import logging
import os
import subprocess
import sys
import zipfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

import mira_engine.observability as diagnostics
from mira_engine.cli.agent_service import AgentPaths, LocalServiceManager


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    instance = diagnostics.RuntimeDiagnostics(tmp_path / "logs", mode="test", capture_faults=False)
    monkeypatch.setattr(diagnostics, "_runtime", instance)
    yield instance
    instance.close()


def rows(runtime):
    return [json.loads(line) for line in runtime.path.read_text().splitlines()]


def test_legacy_logs_and_exception_locals_do_not_persist_private_content(runtime):
    secret = "patient-Alan-private-content sk-secret123456"
    logger.info("Incoming conversation {}", secret)
    logging.warning("External dependency failed with payload %s", secret)
    try:
        raise RuntimeError(secret)
    except RuntimeError as error:
        logger.exception("failure {}", secret)
        diagnostics.emit_event("turn.failed", level="ERROR", exception=error,
                               content=secret, token=secret, request_id="r-1")
    text = runtime.path.read_text()
    assert secret not in text
    assert "patient-Alan" not in text
    events = rows(runtime)
    assert any(event["event"] == "runtime.standard_log" for event in events)
    failure = next(event for event in events if event["event"] == "turn.failed")
    assert failure["exception"]["type"] == "RuntimeError"
    assert failure["exception"]["frames"][-1]["function"] == "test_legacy_logs_and_exception_locals_do_not_persist_private_content"
    assert failure["request_id"] == "r-1"
    assert "content" not in failure and "token" not in failure


@pytest.mark.asyncio
async def test_concurrent_turns_do_not_mix_correlation_ids(runtime):
    async def turn(request_id):
        with diagnostics.diagnostic_context(request_id=request_id):
            await asyncio.sleep(0)
            diagnostics.emit_event("turn.probe", run_id=request_id)
    await asyncio.gather(turn("req-A"), turn("req-B"))
    assert {(event["request_id"],event["run_id"]) for event in rows(runtime)
            if event["event"] == "turn.probe"} == {("req-A", "req-A"), ("req-B", "req-B")}
    assert diagnostics._context.get() == {}


def test_rotation_is_bounded_and_each_retained_line_is_json(tmp_path):
    runtime = diagnostics.RuntimeDiagnostics(tmp_path, mode="test", max_bytes=1500, backups=2, capture_faults=False)
    try:
        for count in range(100):
            runtime.write("rotation.probe", count=count)
        files = list(tmp_path.glob("*.jsonl*"))
        assert len(files) == 3
        assert all(path.stat().st_size < 2000 for path in files)
        for path in files:
            for line in path.read_text().splitlines():
                assert json.loads(line)["schema_version"] == 1
    finally:
        runtime.close()


def test_failed_disk_write_does_not_raise_and_is_counted(runtime, monkeypatch):
    monkeypatch.setattr(runtime._handler, "shouldRollover", lambda record: (_ for _ in ()).throw(OSError("disk full")))
    runtime.write("disk.probe", level="ERROR")
    assert runtime._handler.write_errors == 1


@pytest.mark.asyncio
async def test_http_failures_have_trace_ids_and_no_dynamic_path_or_body(runtime):
    app = web.Application(middlewares=[diagnostics.request_middleware()])
    async def fail(_request):
        raise RuntimeError("private patient payload password=bad-secret")
    app.router.add_get("/items/{name}", fail)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/items/patient-private-name?token=secret", headers={"X-Mira-Request-Id": "req-123"})
        assert response.status == 500
        assert response.headers["X-Mira-Request-Id"] == "req-123"
        assert (await response.json())["request_id"] == "req-123"
    text = runtime.path.read_text()
    assert "patient-private" not in text and "bad-secret" not in text
    failure = next(event for event in rows(runtime) if event["event"] == "http.failed")
    assert failure["route"] == "/items/{name}"
    assert failure["request_id"] == "req-123"


def test_export_includes_runtime_tail_and_excludes_raw_service_output(tmp_path):
    manager = LocalServiceManager(AgentPaths.for_home(tmp_path))
    manager.install_service()
    manager._append_log("test_event", password="secret-value")
    with manager.paths.log_file.open("a") as stream:
        stream.write("Patient identifying raw stdout must not be shared\n")
    directory = manager.paths.root / "logs" / "runtime"
    runtime = diagnostics.RuntimeDiagnostics(directory, mode="test", capture_faults=False)
    runtime.write("export.probe")
    runtime.close()
    _, bundle = manager.export_diagnostics()
    with zipfile.ZipFile(bundle) as archive:
        assert "manifest.json" in archive.namelist()
        assert any(name.startswith("engine-runtime/") for name in archive.namelist())
        text = "\n".join(archive.read(name).decode() for name in archive.namelist())
        assert "export.probe" in text
        assert "secret-value" not in text and "Patient identifying" not in text


def test_export_keeps_long_tracebacks_parseable(tmp_path):
    manager = LocalServiceManager(AgentPaths.for_home(tmp_path))
    manager.install_service()
    runtime = diagnostics.RuntimeDiagnostics(manager.paths.root / "logs" / "runtime", mode="test", capture_faults=False)
    runtime.close()
    record = {"event": "long.stack", "exception": {"type": "Error", "frames": [
        {"file": "frame.py", "function": "long_function_" * 15, "line": line} for line in range(40)
    ]}}
    assert len(json.dumps(record)) > 4096
    with runtime.path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    _, bundle = manager.export_diagnostics()
    with zipfile.ZipFile(bundle) as archive:
        exported = archive.read(f"engine-runtime/{runtime.path.name}.tail").decode()
        parsed = [json.loads(line) for line in exported.splitlines()]
        assert next(row for row in parsed if row["event"] == "long.stack") == record


def test_python_uncaught_exception_is_persisted_before_exit(tmp_path):
    code = """
from pathlib import Path
from mira_engine.observability import configure_runtime_logging
import sys
configure_runtime_logging(directory=Path(sys.argv[1]))
raise RuntimeError('patient-private-token')
"""
    result = subprocess.run([sys.executable,"-c",code,str(tmp_path)], capture_output=True, timeout=15)
    assert result.returncode != 0
    text = next(tmp_path.glob("engine-*.jsonl")).read_text()
    assert "process.uncaught_exception" in text
    assert "patient-private-token" not in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX isolated fatal-signal probe")
def test_native_abort_leaves_python_stack(tmp_path):
    code = """
from pathlib import Path
from mira_engine.observability import configure_runtime_logging
import os, signal, sys
configure_runtime_logging(directory=Path(sys.argv[1]))
os.kill(os.getpid(), signal.SIGABRT)
"""
    result = subprocess.run([sys.executable,"-c",code,str(tmp_path)], capture_output=True, timeout=15)
    assert result.returncode != 0
    fault = next(tmp_path.glob("fault-*.log")).read_text()
    assert "Fatal Python error" in fault
    assert '<module>' in fault


def test_library_warnings_stay_on_stderr_when_root_has_no_handlers(tmp_path, monkeypatch, capsys):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    runtime = diagnostics.RuntimeDiagnostics(tmp_path, mode="test", capture_faults=False)
    try:
        logging.getLogger("aiohttp.server").error("Error handling request")
    finally:
        runtime.close()
    assert "Error handling request" in capsys.readouterr().err
    assert root.handlers == []
    assert any(event["event"] == "runtime.standard_log" for event in rows(runtime))


def test_existing_root_handlers_do_not_get_a_second_stderr_handler(runtime):
    assert runtime._stderr_handler is None


@pytest.mark.asyncio
async def test_asyncio_exception_is_recorded_once(runtime):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    runtime.install_asyncio_handler(loop)
    try:
        loop.call_exception_handler({"message": "Task exception was never retrieved",
                                     "exception": RuntimeError("private")})
    finally:
        loop.set_exception_handler(previous)
    events = [event["event"] for event in rows(runtime)]
    assert events.count("asyncio.unhandled_exception") == 1
    assert "runtime.standard_log" not in events


def _service_log(tmp_path, lines):
    path = tmp_path / "agent-service.log"
    path.write_text("\n".join(lines) + "\n")
    return diagnostics.structured_service_tail(path)


def test_service_tail_only_exports_service_manager_rows(tmp_path):
    lifecycle = {"schema_version": 1, "component": "engine-service", "pid": 4, "request_id": "r-1",
                 "timestamp": "2026-10-13T15:21:03+00:00", "event": "start_service", "running": True}
    exported = _service_log(tmp_path, [
        json.dumps(lifecycle),
        # structlog-style stderr from an MCP server: "event" is free text.
        json.dumps({"event": "Patient John Doe, dx: HIV", "timestamp": "2026-10-13T15:21:03Z"}),
        json.dumps({**lifecycle, "event": "Patient John Doe"}),
        json.dumps({**lifecycle, "timestamp": {"note": "sk-live secret prompt"}}),
        json.dumps({**lifecycle, "component": "mcp"}),
        json.dumps({**lifecycle, "schema_version": 2}),
    ])
    assert [json.loads(line) for line in exported.splitlines()] == [{
        "timestamp": "2026-10-13T15:21:03+00:00", "event": "start_service",
        "request_id": "r-1", "pid": 4, "running": True,
    }]
    assert "Patient" not in exported and "sk-live" not in exported


def test_service_tail_skips_deeply_nested_json(tmp_path):
    lifecycle = {"schema_version": 1, "component": "engine-service",
                 "timestamp": "2026-10-13T15:21:03+00:00", "event": "stop_service"}
    exported = _service_log(tmp_path, ["[" * 50_000, "{" + '"a":[' * 50_000, json.dumps(lifecycle)])
    assert [json.loads(line)["event"] for line in exported.splitlines()] == ["stop_service"]


def test_opaque_ref_is_keyed_per_install(tmp_path):
    first = diagnostics.RuntimeDiagnostics(tmp_path / "a", mode="test", capture_faults=False)
    again = diagnostics.RuntimeDiagnostics(tmp_path / "a", mode="test", capture_faults=False)
    other = diagnostics.RuntimeDiagnostics(tmp_path / "b", mode="test", capture_faults=False)
    try:
        session = "whatsapp:15551234567@s.whatsapp.net"
        import hashlib
        unsalted = hashlib.sha256(session.encode()).hexdigest()[:16]
        ref = diagnostics.opaque_ref(session, key=first.ref_key)
        assert ref != unsalted
        assert ref == diagnostics.opaque_ref(session, key=again.ref_key)
        assert ref != diagnostics.opaque_ref(session, key=other.ref_key)
        key_file = tmp_path / "a" / diagnostics.REF_KEY_FILE
        assert key_file.read_bytes() == first.ref_key
        if os.name != "nt":
            assert key_file.stat().st_mode & 0o777 == 0o600
        assert not list((tmp_path / "a").glob(".ref.key.*"))
    finally:
        for runtime in (first, again, other):
            runtime.close()


def test_opaque_ref_uses_active_runtime_key(runtime):
    assert diagnostics.opaque_ref("ui:s-1") == diagnostics.opaque_ref("ui:s-1", key=runtime.ref_key)


def test_unreadable_ref_key_falls_back_to_process_key(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics.Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(PermissionError("denied")))
    assert diagnostics.load_ref_key(tmp_path) == diagnostics._process_ref_key


def test_corrupt_ref_key_is_replaced(tmp_path):
    (tmp_path / diagnostics.REF_KEY_FILE).write_bytes(b"short")
    key = diagnostics.load_ref_key(tmp_path)
    assert len(key) == 32
    assert diagnostics.load_ref_key(tmp_path) == key


def test_export_tolerates_runtime_file_vanishing_mid_export(tmp_path, monkeypatch):
    manager = LocalServiceManager(AgentPaths.for_home(tmp_path))
    manager.install_service()
    directory = manager.paths.root / "logs" / "runtime"
    runtime = diagnostics.RuntimeDiagnostics(directory, mode="test", capture_faults=False)
    runtime.write("export.probe")
    runtime.close()
    vanished = directory / f"engine-1-{'a' * 32}.jsonl.1"
    vanished.write_text("{}\n")
    real_lstat = diagnostics.Path.lstat

    def lstat(self):
        if self.name == vanished.name:
            raise FileNotFoundError(self)
        return real_lstat(self)

    monkeypatch.setattr(diagnostics.Path, "lstat", lstat)
    code, bundle = manager.export_diagnostics()
    assert code == 0
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert {"file": vanished.name, "error_type": "FileNotFoundError"} in manifest["errors"]
    assert runtime.path.name in manifest["runtime_files"]
    if os.name != "nt":
        assert os.stat(bundle).st_mode & 0o777 == 0o600


def test_failed_export_leaves_no_bundle(tmp_path, monkeypatch):
    manager = LocalServiceManager(AgentPaths.for_home(tmp_path))
    manager.install_service()
    monkeypatch.setattr(manager, "_write_diagnostics_bundle",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        manager.export_diagnostics()
    assert not list((manager.paths.runtime_dir / "diagnostics").glob("*.zip"))


def test_lifecycle_events_from_one_command_share_a_request_id(tmp_path, monkeypatch):
    import mira_engine.cli.agent_service as agent_service

    monkeypatch.delenv("MIRA_DIAGNOSTIC_REQUEST_ID", raising=False)
    monkeypatch.setattr(agent_service, "_lifecycle_request_id", None)
    manager = LocalServiceManager(AgentPaths.for_home(tmp_path))
    manager._append_log("install_service")
    manager._append_log("start_service")
    monkeypatch.setenv("MIRA_DIAGNOSTIC_REQUEST_ID", "desktop-req-9")
    manager._append_log("stop_service")
    ids = [json.loads(line)["request_id"] for line in manager.paths.log_file.read_text().splitlines()]
    assert ids[0] == ids[1] != "desktop-req-9"
    assert ids[2] == "desktop-req-9"
