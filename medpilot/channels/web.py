"""Web channel – exposes a WebSocket + HTTP API for browser/Electron clients."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from medpilot.bus.events import OutboundMessage
from medpilot.bus.queue import MessageBus
from medpilot.channels.base import BaseChannel
from medpilot.config.schema import WebChannelConfig

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
        self._app.router.add_get("/api/plan", self._handle_plan)
        self._app.router.add_post("/api/config", self._handle_config)
        self._app.router.add_get("/api/projects", self._handle_list_projects)
        self._app.router.add_delete("/api/projects", self._handle_delete_project)

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

                if session_id is None:
                    await ws.send_json(
                        {"type": "error", "content": "session_id required"}
                    )
                    continue

                self._clients[session_id] = ws

                project_dir = str(self.projects_root / session_id)
                metadata: dict[str, Any] = {
                    "source": "web",
                    "project_dir": project_dir,
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
        if session_id:
            plan_path = self.projects_root / session_id / PLAN_FILENAME
        else:
            return web.json_response(None)

        if not plan_path.is_file():
            return web.json_response(None)

        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
            return web.json_response(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read {}: {}", plan_path, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_list_projects(self, _request: web.Request) -> web.Response:
        """List project directories under projects_root with optional task_plan data."""
        if not self.projects_root.is_dir():
            return web.json_response({"projects": []})

        projects: list[dict[str, Any]] = []
        for d in sorted(self.projects_root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
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
