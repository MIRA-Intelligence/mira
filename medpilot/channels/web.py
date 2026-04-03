"""Web channel – exposes a WebSocket + HTTP API for browser/Electron clients."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from medpilot.bus.events import OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.agent.skill_plugins import SkillPluginError, SkillPluginManager
from medpilot.channels.base import BaseChannel
from medpilot.config.schema import WebChannelConfig
from medpilot.session.manager import SessionManager

PLAN_FILENAME = "task_plan.json"
_ASSETS_DIR = Path(__file__).parent / "web_assets"


def _load_ui_instructions() -> str:
    """Load AGENTS_UI.md + SKILL_UI.md and return as a single system-prompt block."""
    parts: list[str] = []
    for name in ("AGENTS_UI.md", "SKILL_UI.md"):
        fp = _ASSETS_DIR / name
        if fp.is_file():
            parts.append(fp.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def _normalize_run_mode(value: Any) -> str:
    """Normalize UI run mode with a conservative fallback."""
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in {"manual", "auto"}:
            return mode
    return "manual"


def _stringify_history_content(content: Any) -> str:
    """Flatten session content into a UI-friendly text payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "image_url":
                parts.append("[image]")
        return "\n".join(part for part in parts if part).strip()
    if content is None:
        return ""
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _format_tool_call(tool_call: dict[str, Any]) -> str:
    """Render a tool call in the same compact form shown in logs."""
    fn = tool_call.get("function") if isinstance(tool_call, dict) else None
    if isinstance(fn, dict):
        name = fn.get("name") or "tool"
        args = fn.get("arguments")
    else:
        name = tool_call.get("name") or "tool"
        args = tool_call.get("arguments")

    if isinstance(args, str):
        args_str = args.strip()
    elif args is None:
        args_str = ""
    else:
        args_str = json.dumps(args, ensure_ascii=False)

    return f"{name}({args_str})" if args_str else f"{name}()"


def _load_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _collect_output_artifacts(project_dir: Path, exp_id: str) -> list[str]:
    output_dir = project_dir / "outputs" / exp_id.lower()
    if not output_dir.is_dir():
        return []
    return sorted(
        str(path.relative_to(project_dir))
        for path in output_dir.rglob("*")
        if path.is_file()
    )


def _latest_experiment_commit(project_dir: Path, exp_id: str) -> str | None:
    if not (project_dir / ".git").is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", "--grep", exp_id, "-i", "-n", "1"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    commit = result.stdout.strip().splitlines()
    if not commit:
        return None
    return commit[0][:7]


def _safe_upload_name(filename: str) -> str:
    """Normalize incoming filenames to a basename-only safe value."""
    return Path(filename).name.strip().replace("\x00", "")


def _next_available_path(base_dir: Path, filename: str) -> Path:
    """Return a non-colliding destination path inside *base_dir*."""
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem or "file"
    suffix = Path(filename).suffix
    idx = 1
    while True:
        alt = base_dir / f"{stem}_{idx}{suffix}"
        if not alt.exists():
            return alt
        idx += 1


def _merge_recovered_results(existing: Any, recovered_metrics: Any, artifacts: list[str]) -> dict[str, Any]:
    results = dict(existing) if isinstance(existing, dict) else {}

    if recovered_metrics is not None and "metrics" not in results:
        if isinstance(recovered_metrics, dict) and any(
            key in recovered_metrics for key in ("metrics", "findings", "artifacts")
        ):
            for key, value in recovered_metrics.items():
                results.setdefault(key, value)
        else:
            results["metrics"] = recovered_metrics

    existing_artifacts = results.get("artifacts")
    artifact_list = list(existing_artifacts) if isinstance(existing_artifacts, list) else []
    merged_artifacts = sorted({*artifact_list, *artifacts})
    if merged_artifacts:
        results["artifacts"] = merged_artifacts

    if not results.get("findings") and recovered_metrics is not None:
        results["findings"] = "Recovered experiment output from existing workspace artifacts."

    return results


class WebChannel(BaseChannel):
    """WebSocket + REST channel for frontend clients."""

    name = "web"

    def __init__(self, config: WebChannelConfig, bus: MessageBus, workspace: Path | None = None):
        super().__init__(config, bus)
        self.config: WebChannelConfig = config
        self.workspace: Path | None = workspace
        self.projects_root: Path = Path("~/.medpilot/workspace").expanduser()
        self._ui_instructions: str = _load_ui_instructions()
        self._clients: dict[str, web.WebSocketResponse] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._migrate_global_to_project()

    # ── migration ──────────────────────────────────────────────────

    def _migrate_global_to_project(self) -> None:
        """One-time migration: move global sessions/memory into per-project dirs.

        Scans workspace_root/sessions/ for files named web_PRJ-XXXX.jsonl and
        moves them into PRJ-XXXX/sessions/.  Similarly moves global memory/ into
        the first project that exists (as a best-effort fallback).
        """
        root = self.projects_root
        global_sessions = root / "sessions"
        global_memory = root / "memory"

        if global_sessions.is_dir():
            for f in list(global_sessions.iterdir()):
                if not f.name.endswith(".jsonl"):
                    continue
                stem = f.stem  # e.g. "web_PRJ-0001"
                project_id = stem.replace("web_", "", 1)  # "PRJ-0001"
                proj_dir = root / project_id
                if not proj_dir.is_dir():
                    continue
                dest_dir = proj_dir / "sessions"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / f.name
                if not dest.exists():
                    try:
                        shutil.move(str(f), str(dest))
                        logger.info("Migrated session {} → {}", f.name, dest)
                    except OSError as e:
                        logger.warning("Failed to migrate session {}: {}", f.name, e)
            if not any(global_sessions.iterdir()):
                try:
                    global_sessions.rmdir()
                except OSError:
                    pass

        if global_memory.is_dir():
            projects = [
                d for d in sorted(root.iterdir())
                if d.is_dir() and d.name.startswith("PRJ-")
            ]
            if len(projects) == 1:
                dest_dir = projects[0] / "memory"
                if not dest_dir.exists():
                    try:
                        shutil.move(str(global_memory), str(dest_dir))
                        logger.info("Migrated global memory → {}", dest_dir)
                    except OSError as e:
                        logger.warning("Failed to migrate memory: {}", e)
            elif not projects:
                pass
            else:
                logger.info(
                    "Multiple projects exist; skipping global memory migration. "
                    "Manually move {} into the correct project.",
                    global_memory,
                )

    # ── lifecycle ────────────────────────────────────────────────────

    def _kill_stale_listener(self) -> None:
        """Kill any leftover process occupying our port before binding."""
        import os
        import signal
        import subprocess

        my_pid = os.getpid()
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{self.config.port}"],
                capture_output=True, text=True, timeout=5,
            )
            pids = {
                int(p) for p in result.stdout.split() if p.strip()
            } - {my_pid}
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            return

        for pid in pids:
            try:
                logger.warning("Killing stale process {} on port {}", pid, self.config.port)
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        if pids:
            import time
            time.sleep(0.5)

    async def start(self) -> None:
        self._kill_stale_listener()

        self._app = web.Application(middlewares=[self._cors_middleware])
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/api/status", self._handle_status)
        self._app.router.add_get("/api/sessions", self._handle_sessions)
        self._app.router.add_get("/api/sessions/{session_id}/history", self._handle_history)
        self._app.router.add_get("/api/plan", self._handle_plan)
        self._app.router.add_post("/api/config", self._handle_config)
        self._app.router.add_get("/api/projects", self._handle_list_projects)
        self._app.router.add_delete("/api/projects", self._handle_delete_project)
        self._app.router.add_post("/api/projects/{session_id}/files", self._handle_upload_project_files)
        self._app.router.add_get("/api/projects/{session_id}/artifacts", self._handle_project_artifact)
        self._app.router.add_get("/api/projects/{session_id}/skill-plugins", self._handle_skill_plugins_list)
        self._app.router.add_post("/api/projects/{session_id}/skill-plugins/install", self._handle_skill_plugins_install)
        self._app.router.add_post("/api/projects/{session_id}/skill-plugins/state", self._handle_skill_plugins_state)
        self._app.router.add_delete("/api/projects/{session_id}/skill-plugins/{plugin_id}", self._handle_skill_plugins_uninstall)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self.config.host, self.config.port,
            reuse_address=True,
        )
        await self._site.start()
        self._running = True
        logger.info(
            "Web channel listening on {}:{}",
            self.config.host,
            self.config.port,
        )

        # Keep the channel alive until stopped
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False

        for sid, ws in list(self._clients.items()):
            await ws.close()
        self._clients.clear()

        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("Web channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        ws = self._clients.get(msg.chat_id)
        if ws is None or ws.closed:
            logger.debug("No active WebSocket for chat_id={}", msg.chat_id)
            return

        is_progress = msg.metadata.get("_progress", False)
        payload = {
            "type": "progress" if is_progress else "response",
            "session_id": msg.chat_id,
            "content": msg.content,
            "media": msg.media,
            "metadata": msg.metadata,
        }

        try:
            await ws.send_json(payload)
        except Exception as e:
            logger.warning("Failed to send to {}: {}", msg.chat_id, e)

    def _reconcile_plan_data(self, project_dir: Path, data: dict[str, Any]) -> bool:
        experiments = data.get("experiments")
        if not isinstance(experiments, list):
            return False

        changed = False
        for exp in experiments:
            if not isinstance(exp, dict):
                continue

            exp_id = exp.get("id")
            if not isinstance(exp_id, str) or not exp_id:
                continue

            results_path = project_dir / "outputs" / exp_id.lower() / "results.json"
            recovered_metrics = _load_json_file(results_path) if results_path.is_file() else None
            if recovered_metrics is None:
                continue

            if exp.get("status") != "completed":
                exp["status"] = "completed"
                changed = True

            merged_results = _merge_recovered_results(
                exp.get("results"),
                recovered_metrics,
                _collect_output_artifacts(project_dir, exp_id),
            )
            if exp.get("results") != merged_results:
                exp["results"] = merged_results
                changed = True

            if not exp.get("commit"):
                commit = _latest_experiment_commit(project_dir, exp_id)
                if commit:
                    exp["commit"] = commit
                    changed = True

            if not exp.get("conclusion"):
                exp["conclusion"] = "Recovered completed state from existing experiment artifacts."
                changed = True

        if not any(isinstance(exp, dict) and exp.get("status") == "running" for exp in experiments):
            pending = next(
                (exp.get("id") for exp in experiments if isinstance(exp, dict) and exp.get("status") == "pending"),
                None,
            )
            current = data.get("current_experiment")
            if pending and current != pending:
                data["current_experiment"] = pending
                changed = True

        return changed

    def _load_plan_data(self, session_id: str, *, reconcile: bool = True) -> dict[str, Any] | None:
        project_dir = self.projects_root / session_id
        plan_path = project_dir / PLAN_FILENAME
        if not plan_path.is_file():
            return None

        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Failed to read {plan_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected non-object JSON in {plan_path}")

        if reconcile and self._reconcile_plan_data(project_dir, data):
            try:
                plan_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("Failed to write reconciled {}: {}", plan_path, exc)

        return data

    # ── CORS middleware ──────────────────────────────────────────────

    @web.middleware
    async def _cors_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)

        origin = request.headers.get("Origin", "*")
        allowed = self.config.cors_origins
        if "*" in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
        elif origin in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin

        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    # ── WebSocket handler ────────────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session_id: str | None = None

        async for raw in ws:
            if raw.type != web.WSMsgType.TEXT:
                continue

            try:
                data: dict = json.loads(raw.data)
            except (json.JSONDecodeError, TypeError):
                await ws.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            msg_type = data.get("type")

            if msg_type == "message":
                session_id = data.get("session_id", session_id)
                user_id = data.get("user_id", session_id or "anonymous")
                content = data.get("content", "")
                media = data.get("media", [])
                run_mode = _normalize_run_mode(data.get("mode"))

                if session_id is None:
                    await ws.send_json(
                        {"type": "error", "content": "session_id required"}
                    )
                    continue

                self._clients[session_id] = ws
                try:
                    self._load_plan_data(session_id)
                except ValueError as exc:
                    logger.warning(str(exc))

                project_dir = str(self.projects_root / session_id)
                metadata: dict[str, Any] = {
                    "source": "web",
                    "project_dir": project_dir,
                    "run_mode": run_mode,
                }
                if self._ui_instructions:
                    metadata["_ui_system_instructions"] = self._ui_instructions
                await self._handle_message(
                    sender_id=user_id,
                    chat_id=session_id,
                    content=content,
                    media=media,
                    metadata=metadata,
                    session_key=f"web:{session_id}",
                )

        # Client disconnected
        if session_id and self._clients.get(session_id) is ws:
            del self._clients[session_id]
            logger.info("WebSocket client disconnected: {}", session_id)

        return ws

    # ── REST endpoints ───────────────────────────────────────────────

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "channel": self.name,
            "running": self._running,
            "connected_clients": len(self._clients),
            "uptime_host": f"{self.config.host}:{self.config.port}",
            "projects_root": str(self.projects_root),
        })

    async def _handle_sessions(self, _request: web.Request) -> web.Response:
        sessions = [
            {"session_id": sid, "connected": not ws.closed}
            for sid, ws in self._clients.items()
        ]
        return web.json_response({"sessions": sessions})

    def _load_history_entries(self, session_id: str) -> list[dict[str, Any]]:
        project_dir = self.projects_root / session_id
        if not project_dir.is_dir():
            return []

        session = SessionManager(project_dir).get_or_create(f"web:{session_id}")
        entries: list[dict[str, Any]] = []

        for idx, msg in enumerate(session.messages):
            timestamp = msg.get("timestamp") or ""
            role = msg.get("role")

            if role == "user":
                content = _stringify_history_content(msg.get("content"))
                if not content:
                    continue
                entries.append({
                    "id": f"history-{session_id}-{idx}-user",
                    "timestamp": timestamp,
                    "content": content,
                    "type": "response",
                    "metadata": {"_user": True},
                })
                continue

            if role == "assistant":
                content = _stringify_history_content(msg.get("content"))
                if content:
                    entries.append({
                        "id": f"history-{session_id}-{idx}-assistant",
                        "timestamp": timestamp,
                        "content": content,
                        "type": "response",
                        "metadata": {},
                    })

                for tool_idx, tool_call in enumerate(msg.get("tool_calls") or []):
                    if not isinstance(tool_call, dict):
                        continue
                    entries.append({
                        "id": f"history-{session_id}-{idx}-tool-{tool_idx}",
                        "timestamp": timestamp,
                        "content": _format_tool_call(tool_call),
                        "type": "tool_call",
                        "metadata": {},
                    })

        return entries

    async def _handle_history(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        return web.json_response({
            "session_id": session_id,
            "entries": self._load_history_entries(session_id),
        })

    async def _handle_config(self, request: web.Request) -> web.Response:
        """Allow the UI to configure the projects root path."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)

        if "projects_root" in body:
            new_root = Path(body["projects_root"]).expanduser().resolve()
            self.projects_root = new_root
            logger.info("Projects root updated to {}", new_root)

        return web.json_response({
            "projects_root": str(self.projects_root),
        })

    async def _handle_plan(self, request: web.Request) -> web.Response:
        """Serve task_plan.json, scoped to a project when session_id is given."""
        session_id = request.query.get("session_id")
        if not session_id:
            return web.json_response(None)

        try:
            data = self._load_plan_data(session_id)
        except ValueError as exc:
            logger.warning(str(exc))
            return web.json_response({"error": str(exc)}, status=500)
        if data is None:
            return web.json_response(None)
        return web.json_response(data)

    _NON_PROJECT_DIRS = {"skills", "memory", "sessions", "media", "cron", "logs"}

    async def _handle_list_projects(self, _request: web.Request) -> web.Response:
        """List project directories under projects_root with optional task_plan data."""
        if not self.projects_root.is_dir():
            return web.json_response({"projects": []})

        projects: list[dict[str, Any]] = []
        for d in sorted(self.projects_root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.name in self._NON_PROJECT_DIRS:
                continue
            info: dict[str, Any] = {"id": d.name}
            plan_file = d / PLAN_FILENAME
            if plan_file.is_file():
                try:
                    plan = json.loads(plan_file.read_text(encoding="utf-8"))
                    info["title"] = plan.get("title", "")
                    info["status"] = plan.get("status", "in_progress")
                    info["core_question"] = plan.get("core_question", "")
                    info["started_at"] = plan.get("started_at", "")
                    info["has_plan"] = True
                except (json.JSONDecodeError, OSError):
                    info["has_plan"] = False
            else:
                info["has_plan"] = False
            projects.append(info)

        return web.json_response({"projects": projects})

    async def _handle_delete_project(self, request: web.Request) -> web.Response:
        """Delete a project directory from disk."""
        session_id = request.query.get("session_id")
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        project_dir = self.projects_root / session_id
        if not project_dir.is_dir():
            return web.json_response({"deleted": False, "reason": "not found"})

        try:
            shutil.rmtree(project_dir)
            logger.info("Deleted project directory: {}", project_dir)
            return web.json_response({"deleted": True})
        except OSError as exc:
            logger.warning("Failed to delete {}: {}", project_dir, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_upload_project_files(self, request: web.Request) -> web.Response:
        """Upload files into projects_root/<session_id>/data for web clients."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        try:
            multipart = await request.multipart()
        except Exception:
            return web.json_response({"error": "expected multipart/form-data"}, status=400)

        project_dir = self.projects_root / session_id
        data_dir = project_dir / "data"
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Failed to create upload directory {}: {}", data_dir, exc)
            return web.json_response({"error": str(exc)}, status=500)

        uploaded: list[dict[str, Any]] = []

        while True:
            part = await multipart.next()
            if part is None:
                break
            if part.name != "files":
                await part.release()
                continue
            if not part.filename:
                await part.release()
                continue

            safe_name = _safe_upload_name(part.filename)
            if not safe_name:
                await part.release()
                continue

            target = _next_available_path(data_dir, safe_name)
            size = 0
            try:
                with target.open("wb") as f:
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        f.write(chunk)
                        size += len(chunk)
            except OSError as exc:
                logger.warning("Failed to write uploaded file {}: {}", target, exc)
                return web.json_response({"error": str(exc)}, status=500)

            uploaded.append({
                "name": target.name,
                "path": str(target.relative_to(project_dir)),
                "size": size,
            })

        if not uploaded:
            return web.json_response({"error": "no files uploaded"}, status=400)

        return web.json_response({
            "session_id": session_id,
            "uploaded": uploaded,
        })

    async def _handle_project_artifact(self, request: web.Request) -> web.Response:
        """Serve a project file under projects_root/<session_id> by relative path."""
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)

        rel_path = request.query.get("path", "").strip()
        if not rel_path:
            return web.json_response({"error": "path required"}, status=400)

        project_dir = (self.projects_root / session_id).resolve()
        if not project_dir.is_dir():
            return web.json_response({"error": "project not found"}, status=404)

        candidate = (project_dir / rel_path).resolve()
        try:
            candidate.relative_to(project_dir)
        except ValueError:
            return web.json_response({"error": "invalid artifact path"}, status=400)

        if not candidate.is_file():
            return web.json_response({"error": "artifact not found"}, status=404)

        return web.FileResponse(candidate)

    def _skill_plugin_manager(self, session_id: str) -> SkillPluginManager:
        project_dir = self.projects_root / session_id
        project_dir.mkdir(parents=True, exist_ok=True)
        return SkillPluginManager(project_dir)

    async def _handle_skill_plugins_list(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        manager = self._skill_plugin_manager(session_id)
        return web.json_response({"plugins": manager.list_plugins()})

    async def _handle_skill_plugins_install(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        manager = self._skill_plugin_manager(session_id)

        content_type = request.headers.get("Content-Type", "").lower()
        try:
            if content_type.startswith("multipart/form-data"):
                multipart = await request.multipart()
                zip_path: Path | None = None
                zip_name: str | None = None
                while True:
                    part = await multipart.next()
                    if part is None:
                        break
                    if part.name != "zip":
                        await part.release()
                        continue
                    if not part.filename:
                        await part.release()
                        continue
                    zip_name = part.filename
                    with tempfile.NamedTemporaryFile(
                        prefix="skill-plugin-",
                        suffix=".zip",
                        delete=False,
                    ) as tmp:
                        while True:
                            chunk = await part.read_chunk()
                            if not chunk:
                                break
                            tmp.write(chunk)
                        zip_path = Path(tmp.name)
                if zip_path is None:
                    return web.json_response({"error": "zip file field 'zip' is required"}, status=400)
                try:
                    installed = manager.install_from_zip(zip_path, archive_name_hint=zip_name)
                finally:
                    try:
                        zip_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                try:
                    body = await request.json()
                except (json.JSONDecodeError, TypeError):
                    return web.json_response({"error": "invalid JSON"}, status=400)
                source_path = body.get("path") if isinstance(body, dict) else None
                if not isinstance(source_path, str) or not source_path.strip():
                    return web.json_response({"error": "directory path is required"}, status=400)
                installed = manager.install_from_directory(Path(source_path))
        except SkillPluginError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({
            "installed": installed,
            "plugins": manager.list_plugins(),
        })

    async def _handle_skill_plugins_state(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError):
            return web.json_response({"error": "invalid JSON"}, status=400)

        scope = body.get("scope")
        target_type = body.get("target_type")
        plugin_id = body.get("plugin_id")
        enabled = body.get("enabled")
        target_id = body.get("target_id")
        if not isinstance(enabled, bool):
            return web.json_response({"error": "enabled must be a boolean"}, status=400)

        manager = self._skill_plugin_manager(session_id)
        try:
            manager.set_enabled(
                scope=scope,
                plugin_id=plugin_id,
                target_type=target_type,
                enabled=enabled,
                target_id=target_id,
            )
        except SkillPluginError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({"plugins": manager.list_plugins()})

    async def _handle_skill_plugins_uninstall(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        plugin_id = request.match_info.get("plugin_id", "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        if not plugin_id:
            return web.json_response({"error": "plugin_id required"}, status=400)

        manager = self._skill_plugin_manager(session_id)
        try:
            manager.uninstall(plugin_id)
        except SkillPluginError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({"uninstalled": plugin_id, "plugins": manager.list_plugins()})
