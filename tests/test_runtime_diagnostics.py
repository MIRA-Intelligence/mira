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
