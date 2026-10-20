"""Local, bounded runtime diagnostics. Never persist prompts or exception locals."""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import faulthandler
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

SCHEMA_VERSION = 1
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
MAX_RUNTIME_FILES = 32
MAX_TAIL_BYTES = 128 * 1024
REF_KEY_FILE = "ref.key"
_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("mira_diagnostics", default={})
# Set while asyncio's default handler logs an exception already recorded as an event.
_mirrored: contextvars.ContextVar[bool] = contextvars.ContextVar("mira_diagnostics_mirrored", default=False)
_runtime: RuntimeDiagnostics | None = None
# Used only when no runtime directory holds a per-install key; refs then stay per process.
_process_ref_key = os.urandom(32)
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_SERVICE_EVENT = re.compile(r"[a-z][a-z0-9_.]{0,63}\Z")
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_FIELDS = frozenset({
    "request_id", "run_id", "connection_id", "session_ref", "workflow_run_id",
    "channel", "tool", "method", "route", "status", "duration_ms", "outcome",
    "host", "port", "mode", "reason", "code", "count", "operation", "phase",
    "service_mode", "platform", "running", "installed", "pid", "signal",
})
_SECRET_KEY = re.compile(r"token|secret|password|authorization|cookie|api.?key|credential", re.I)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[^\s,\"']+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"((?:api[_-]?key|token|secret|password|authorization|cookie)\s*[=:]\s*)[^\s,;]+"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def correlation_id(value: Any = None) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else uuid.uuid4().hex


def opaque_ref(value: Any, *, key: bytes | None = None) -> str:
    """Keyed so exported refs cannot be brute-forced back to phone numbers or paths."""
    if key is None:
        key = _runtime.ref_key if _runtime is not None else _process_ref_key
    return hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()[:16]


def load_ref_key(directory: Path) -> bytes:
    """Return the per-install ref key, creating it atomically so racing processes agree."""
    path = Path(directory) / REF_KEY_FILE
    try:
        key = path.read_bytes()
        if len(key) == 32:
            return key
    except FileNotFoundError:
        pass
    except OSError:
        return _process_ref_key
    key = os.urandom(32)
    temp = path.with_name(f".{REF_KEY_FILE}.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # link() never replaces a key another process published first.
            os.link(temp, path)
        except FileExistsError:
            existing = path.read_bytes()
            if len(existing) == 32:
                return existing
            os.replace(temp, path)
        except OSError:
            os.replace(temp, path)
        return key
    except OSError:
        return _process_ref_key
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Defense in depth for machine status, never a sanitizer for patient prose."""
    if _depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k): "[redacted]" if _SECRET_KEY.search(str(k)) else redact(v, _depth=_depth + 1)
                for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth=_depth + 1) for v in value[:100]]
    if isinstance(value, str):
        value = value.replace(str(Path.home()), "<home>")
        return _SECRET_TEXT.sub("[redacted]", value)[:4096]
    return value if value is None or isinstance(value, (bool, int, float)) else type(value).__name__


def safe_exception(exc: BaseException, *, depth: int = 0) -> dict[str, Any]:
    """Keep code locations and exception types, never messages, locals or source text."""
    frames = []
    tb = exc.__traceback__
    while tb is not None and len(frames) < 40:
        frames.append({"file": Path(tb.tb_frame.f_code.co_filename).name,
                       "function": tb.tb_frame.f_code.co_name, "line": tb.tb_lineno})
        tb = tb.tb_next
    result: dict[str, Any] = {"type": type(exc).__name__, "frames": frames}
    cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and cause is not exc and depth < 3:
        result["cause"] = safe_exception(cause, depth=depth + 1)
    return result


def safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in fields.items():
        if key not in _FIELDS or not isinstance(value, (str, bool, int, float, type(None))):
            continue
        if isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9_./:{}<>-]{1,200}", value):
            continue
        result[key] = redact(value)
    return result


@contextmanager
def diagnostic_context(**fields: Any) -> Iterator[dict[str, Any]]:
    values = {**_context.get(), **safe_fields(fields)}
    token = _context.set(values)
    try:
        yield values
    finally:
        _context.reset(token)


def emit_event(event: str, *, level: str = "INFO", exception: BaseException | None = None,
               **fields: Any) -> None:
    if _runtime is not None:
        _runtime.write(event, level=level, exception=exception, **fields)


def read_tail(path: Path, limit: int = MAX_TAIL_BYTES) -> str:
    """Bound reads even when a legacy service log has grown without rotation."""
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - limit)
        stream.seek(offset)
        data = stream.read(limit)
    if offset:
        data = data.partition(b"\n")[2]
    return data.decode("utf-8", errors="replace")


def structured_service_tail(path: Path) -> str:
    """Old stderr may contain user content. Export only lifecycle JSON metadata.

    Raw stderr from MCP servers or scripts can itself be JSON with an ``event`` key
    (structlog's default), so only the service manager's own schema is accepted.
    """
    rows = []
    for line in read_tail(path).splitlines()[-200:]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            continue
        if not (isinstance(row, dict) and row.get("component") == "engine-service"
                and row.get("schema_version") == SCHEMA_VERSION):
            continue
        event, timestamp = row.get("event"), row.get("timestamp")
        if not (isinstance(event, str) and _SERVICE_EVENT.fullmatch(event)
                and isinstance(timestamp, str) and _ISO_TIMESTAMP.fullmatch(timestamp)):
            continue
        rows.append(json.dumps({"timestamp": timestamp, "event": event, **safe_fields(row)},
                               ensure_ascii=False))
    return "\n".join(rows) + ("\n" if rows else "")


class DiagnosticFileHandler(RotatingFileHandler):
    """Count write failures instead of logging's stderr-only error reporting."""
    write_errors = 0

    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)

    def handleError(self, record: logging.LogRecord) -> None:
        self.write_errors += 1


class StandardLogHandler(logging.Handler):
    def __init__(self, runtime: RuntimeDiagnostics) -> None:
        super().__init__(logging.WARNING)
        self.runtime = runtime

    def emit(self, record: logging.LogRecord) -> None:
        if _mirrored.get():
            return
        self.runtime.write("runtime.standard_log", level=record.levelname,
                           exception=record.exc_info[1] if record.exc_info else None,
                           reason=f"{record.name}:{record.funcName}:{record.lineno}")


class RuntimeDiagnostics:
    def __init__(self, directory: Path, *, mode: str, max_bytes: int = MAX_LOG_BYTES,
                 backups: int = LOG_BACKUPS, capture_faults: bool = True) -> None:
        from mira_engine import __version__

        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.boot_id = uuid.uuid4().hex
        self.path = self.directory / f"engine-{os.getpid()}-{self.boot_id}.jsonl"
        self.ref_key = load_ref_key(self.directory)
        self.identity = {"schema_version": SCHEMA_VERSION, "component": "engine",
                         "boot_id": self.boot_id, "pid": os.getpid(),
                         "version": __version__, "mode": mode,
                         "instance_ref": opaque_ref(self.directory.resolve(), key=self.ref_key)}
        self.write_errors = 0
        self._closed = False
        self._handler = DiagnosticFileHandler(self.path, maxBytes=max_bytes, backupCount=backups,
                                           encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self.path.chmod(0o600)
        self._fault_file = None
        self._sink_id = logger.add(self._loguru_sink, level="INFO", diagnose=False, backtrace=False,
                                   catch=True)
        self._standard_handler = StandardLogHandler(self)
        root = logging.getLogger()
        # A root handler disables logging.lastResort. Keep library warnings visible on
        # stderr (and so in agent-service.log) as they were before diagnostics started.
        self._stderr_handler: logging.Handler | None = None
        if not root.handlers:
            self._stderr_handler = logging.StreamHandler(sys.stderr)
            self._stderr_handler.setLevel(logging.WARNING)
            root.addHandler(self._stderr_handler)
        root.addHandler(self._standard_handler)
        self._old_sys_hook = sys.excepthook
        self._old_thread_hook = threading.excepthook
        self._sys_hook = self._on_uncaught
        self._thread_hook = self._on_thread_error
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook
        if capture_faults:
            try:
                fault_path = self.directory / f"fault-{os.getpid()}-{self.boot_id}.log"
                self._fault_file = fault_path.open("a", encoding="utf-8")
                fault_path.chmod(0o600)
                faulthandler.enable(file=self._fault_file, all_threads=True)
            except (OSError, RuntimeError):
                self.write("diagnostics.fault_capture_unavailable", level="WARNING")
        self._prune_inactive_files()
        self.write("process.started")

    def _prune_inactive_files(self) -> None:
        import psutil

        try:
            files = sorted((p for p in self.directory.iterdir()
                            if re.fullmatch(r"(?:engine|fault)-\d+-[a-f0-9]{32}\.(?:jsonl|log)(?:\.\d+)?", p.name)
                            and p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime,
                           reverse=True)
            for path in files[MAX_RUNTIME_FILES:]:
                pid = int(path.name.split("-")[1])
                if not psutil.pid_exists(pid):
                    path.unlink(missing_ok=True)
        except OSError:
            self.write_errors += 1

    def write(self, event: str, *, level: str = "INFO", exception: BaseException | None = None,
              **fields: Any) -> None:
        if self._closed:
            return
        payload = {**self.identity, "timestamp": utc_now(), "level": level,
                   "event": event, **_context.get(), **safe_fields(fields)}
        if exception is not None:
            payload["exception"] = safe_exception(exception)
        try:
            record = logging.LogRecord("mira", logging.INFO, "", 0,
                                       json.dumps(payload, ensure_ascii=False), (), None)
            # handle() serializes rollover and writes across threads. No async queue
            # can lose the final event when a process exits unexpectedly.
            self._handler.handle(record)
            if level in {"ERROR", "CRITICAL"} and self._handler.stream is not None:
                os.fsync(self._handler.stream.fileno())
        except (OSError, ValueError):
            self.write_errors += 1

    def _loguru_sink(self, message: Any) -> None:
        record = message.record
        exc = record["exception"]
        # Legacy log strings can embed prompts, filenames, tool arguments or patient
        # information. Keep their location and severity; new diagnostic events carry
        # explicit allowlisted metadata instead of copying arbitrary prose.
        self.write("runtime.log", level=record["level"].name,
                   exception=exc.value if exc is not None else None,
                   **{"reason": f"{record['name']}:{record['function']}:{record['line']}"})

    def _on_uncaught(self, exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        self.write("process.uncaught_exception", level="CRITICAL", exception=exc)
        self._old_sys_hook(exc_type, exc, tb)

    def _on_thread_error(self, args: Any) -> None:
        self.write("thread.uncaught_exception", level="ERROR", exception=args.exc_value)
        self._old_thread_hook(args)

    def install_asyncio_handler(self, loop: asyncio.AbstractEventLoop) -> None:
        previous = loop.get_exception_handler()

        def handler(active_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            self.write("asyncio.unhandled_exception", level="ERROR",
                       exception=context.get("exception"))
            token = _mirrored.set(True)
            try:
                if previous is not None:
                    previous(active_loop, context)
                else:
                    active_loop.default_exception_handler(context)
            finally:
                _mirrored.reset(token)

        loop.set_exception_handler(handler)

    def close(self) -> None:
        if self._closed:
            return
        self.write("process.logging_stopped", count=self.write_errors + self._handler.write_errors)
        self._closed = True
        logger.remove(self._sink_id)
        logging.getLogger().removeHandler(self._standard_handler)
        if self._stderr_handler is not None:
            logging.getLogger().removeHandler(self._stderr_handler)
        if sys.excepthook is self._sys_hook:
            sys.excepthook = self._old_sys_hook
        if threading.excepthook is self._thread_hook:
            threading.excepthook = self._old_thread_hook
        if self._fault_file is not None:
            faulthandler.disable()
            self._fault_file.close()
        self._handler.close()


def configure_runtime_logging(*, mode: str = "gateway", directory: Path | None = None) -> RuntimeDiagnostics | None:
    global _runtime
    from mira_engine.config.loader import get_config_path

    target = Path(directory) if directory is not None else get_config_path().parent / "logs" / "runtime"
    if _runtime is not None and not _runtime._closed and _runtime.directory == target:
        return _runtime
    if _runtime is not None:
        _runtime.close()
    try:
        _runtime = RuntimeDiagnostics(target, mode=mode)
    except OSError:
        # A broken log disk must not prevent the app from starting.
        _runtime = None
        sys.stderr.write("MIRA runtime diagnostics unavailable: cannot open log directory.\n")
    if _runtime is not None:
        atexit.register(_runtime.close)
    return _runtime


def request_middleware():
    from aiohttp import web

    @web.middleware
    async def middleware(request: Any, handler: Any) -> Any:
        request_id = correlation_id(request.headers.get("X-Mira-Request-Id"))
        request["diagnostic_request_id"] = request_id
        route = getattr(request.match_info.route.resource, "canonical", None) or "<unmatched>"
        started = time.monotonic()
        status = 500
        with diagnostic_context(request_id=request_id):
            try:
                response = await handler(request)
                status = response.status
                if not response.prepared:
                    response.headers["X-Mira-Request-Id"] = request_id
                return response
            except web.HTTPException as exc:
                status = exc.status
                exc.headers["X-Mira-Request-Id"] = request_id
                raise
            except asyncio.CancelledError:
                status = 499
                raise
            except Exception as exc:
                emit_event("http.failed", level="ERROR", exception=exc,
                           method=request.method, route=route)
                if request.headers.get("Upgrade", "").lower() == "websocket":
                    raise
                response = web.json_response({"error": "Internal server error", "request_id": request_id}, status=500)
                response.headers["X-Mira-Request-Id"] = request_id
                return response
            finally:
                # WebSocket lifetime is logged separately; omit frequent healthy probes.
                if request.path not in {"/health", "/api/health", "/version", "/api/version"} or status >= 400:
                    emit_event("http.completed", level="ERROR" if status >= 500 else "INFO",
                               method=request.method, route=route, status=status,
                               duration_ms=round((time.monotonic() - started) * 1000, 2))
    return middleware
