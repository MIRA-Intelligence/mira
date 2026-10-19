"""aiohttp adapter for the versioned modular workflow contract."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from aiohttp import WSMsgType, web

from mira_engine.projects import ProjectRegistry
from mira_engine.utils.slide_decks import deck_response, is_deck_file

from .journals import PROFILE_VERSION, journal_catalog
from .models import WORKFLOW_CONTRACT_VERSION, WorkflowEvent
from .repository import JsonWorkflowRepository
from .service import WorkflowError, WorkflowService, error_payload


@dataclass
class _Subscriber:
    subscription_id: str
    project_id: str
    ws: web.WebSocketResponse
    include_transient: bool = True
    replaying: bool = True
    buffered: list[WorkflowEvent] = field(default_factory=list)
    last_sent: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WorkflowHttpApi:
    """Keep workflow HTTP/WS concerns out of the legacy UI channel handler."""

    def __init__(self, registry: ProjectRegistry):
        self.registry = registry
        self._subscribers: dict[str, dict[str, _Subscriber]] = defaultdict(dict)
        self._socket_subscriptions: dict[web.WebSocketResponse, set[tuple[str, str]]] = defaultdict(set)

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/workflow/v1/contract", self.contract)
        app.router.add_get("/api/workflow/v1/journals", self.journals)
        app.router.add_get("/api/workflow/v1/projects/{project_id}", self.project_snapshot)
        app.router.add_post(
            "/api/workflow/v1/projects/{project_id}/commands", self.project_command
        )
        app.router.add_get("/api/workflow/v1/projects/{project_id}/runs", self.list_runs)
        app.router.add_post("/api/workflow/v1/projects/{project_id}/runs", self.create_run)
        app.router.add_get(
            "/api/workflow/v1/projects/{project_id}/runs/{run_id}", self.get_run
        )
        app.router.add_post(
            "/api/workflow/v1/projects/{project_id}/runs/{run_id}/commands",
            self.run_command,
        )
        app.router.add_post(
            "/api/workflow/v1/projects/{project_id}/runs/{run_id}/signals",
            self.run_signal,
        )
        app.router.add_get(
            "/api/workflow/v1/projects/{project_id}/runs/{run_id}/checkpoints/{checkpoint_id}",
            self.get_checkpoint,
        )
        app.router.add_get("/api/workflow/v1/projects/{project_id}/events", self.list_events)
        app.router.add_get("/api/workflow/v1/projects/{project_id}/artifacts", self.list_artifacts)
        app.router.add_post("/api/workflow/v1/projects/{project_id}/artifacts", self.import_artifact)
        app.router.add_get(
            "/api/workflow/v1/projects/{project_id}/artifacts/{artifact_id}",
            self.get_artifact,
        )
        app.router.add_get(
            "/api/workflow/v1/projects/{project_id}/artifacts/{artifact_id}/content",
            self.get_artifact_content,
        )
        app.router.add_get("/ws/workflow/v1", self.websocket)

    async def journals(self, _request: web.Request) -> web.Response:
        return web.json_response({"profile_version": PROFILE_VERSION, "journals": journal_catalog()})

    def _service(self, project_id: str) -> WorkflowService:
        ref = self.registry.resolve(project_id)
        repository = JsonWorkflowRepository(ref.project_dir, ref.project_id)
        return WorkflowService(repository, on_event=self._schedule_event)

    def _schedule_event(self, event: WorkflowEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._broadcast(event))

    @staticmethod
    def _json(model: Any, *, status: int = 200) -> web.Response:
        payload = model.model_dump(mode="json", by_alias=True) if hasattr(model, "model_dump") else model
        return web.json_response(payload, status=status)

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(
                text='{"error":{"code":"invalid_json","message":"JSON object required","details":{}}}',
                content_type="application/json",
            ) from exc
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(
                text='{"error":{"code":"invalid_json","message":"JSON object required","details":{}}}',
                content_type="application/json",
            )
        return body

    async def contract(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "transport_version": "v1",
                "workflow_contract_version": WORKFLOW_CONTRACT_VERSION,
                "minimum_client_contract_version": 1,
                "maximum_client_contract_version": 1,
                "features": [
                    "project_event_replay",
                    "run_commands",
                    "structured_interactions",
                    "direct_module_entry",
                ],
                "module_types": ["initiation", "planning", "experiment", "delivery"],
                "event_retention": {"mode": "full_project_history"},
            }
        )

    async def project_snapshot(self, request: web.Request) -> web.Response:
        try:
            service = self._service(request.match_info["project_id"])
            workflow, runs = service.snapshot()
            return web.json_response(
                {
                    "workflow_contract_version": 1,
                    "project_id": service.repository.project_id,
                    "workflow": workflow.model_dump(mode="json"),
                    "runs": [run.model_dump(mode="json", by_alias=True) for run in runs],
                    "project_sequence": workflow.last_project_sequence,
                }
            )
        except Exception as exc:
            return self._error(exc)

    async def list_runs(self, request: web.Request) -> web.Response:
        try:
            runs = self._service(request.match_info["project_id"]).repository.list_runs()
            module_type = request.query.get("module_type")
            status = request.query.get("status")
            if module_type:
                runs = [run for run in runs if run.module_type == module_type]
            if status:
                runs = [run for run in runs if run.status == status]
            return web.json_response({"runs": [run.model_dump(mode="json", by_alias=True) for run in runs]})
        except Exception as exc:
            return self._error(exc)

    async def project_command(self, request: web.Request) -> web.Response:
        try:
            body = await self._body(request)
            receipt = self._service(request.match_info["project_id"]).command_project(
                command_id=str(body.get("command_id") or ""),
                command_type=str(body.get("command_type") or ""),
                expected_revision=body.get("expected_revision")
                if isinstance(body.get("expected_revision"), int)
                else None,
                payload=body.get("payload") if isinstance(body.get("payload"), dict) else None,
            )
            return self._json(receipt)
        except Exception as exc:
            return self._error(exc)

    async def create_run(self, request: web.Request) -> web.Response:
        try:
            body = await self._body(request)
            receipt = self._service(request.match_info["project_id"]).create_run(
                command_id=str(body.get("command_id") or ""),
                module_type=str(body.get("module_type") or ""),
                label=body.get("label") if isinstance(body.get("label"), str) else None,
                inputs=body.get("inputs") if isinstance(body.get("inputs"), list) else None,
                module_data=body.get("module_data") if isinstance(body.get("module_data"), dict) else None,
                created_by=body.get("actor") if isinstance(body.get("actor"), dict) else None,
                enqueue=bool(body.get("enqueue")),
                expected_revision=body.get("expected_revision") if isinstance(body.get("expected_revision"), int) else None,
            )
            return self._json(receipt, status=201 if receipt.disposition != "duplicate" else 200)
        except Exception as exc:
            return self._error(exc)

    async def get_run(self, request: web.Request) -> web.Response:
        try:
            run = self._service(request.match_info["project_id"]).repository.load_run(
                request.match_info["run_id"]
            )
            return self._json(run)
        except Exception as exc:
            return self._error(exc)

    async def run_command(self, request: web.Request) -> web.Response:
        try:
            body = await self._body(request)
            receipt = self._service(request.match_info["project_id"]).command_run(
                request.match_info["run_id"],
                command_id=str(body.get("command_id") or ""),
                command_type=str(body.get("command_type") or ""),
                expected_revision=body.get("expected_revision") if isinstance(body.get("expected_revision"), int) else None,
                payload=body.get("payload") if isinstance(body.get("payload"), dict) else None,
            )
            status = 202 if receipt.disposition in {"accepted", "pending_safe_boundary"} else 200
            return self._json(receipt, status=status)
        except Exception as exc:
            return self._error(exc)

    async def run_signal(self, request: web.Request) -> web.Response:
        try:
            body = await self._body(request)
            service = self._service(request.match_info["project_id"])
            signal_type = body.get("signal_type")
            common = {
                "run_id": request.match_info["run_id"],
                "command_id": str(body.get("command_id") or ""),
                "checkpoint_state": body.get("checkpoint_state")
                if isinstance(body.get("checkpoint_state"), dict)
                else None,
                "conversation_cursor": body.get("conversation_cursor")
                if isinstance(body.get("conversation_cursor"), (str, int))
                else None,
                "attempt_id": body.get("attempt_id")
                if isinstance(body.get("attempt_id"), str)
                else None,
            }
            if signal_type == "interaction_requested":
                interaction = body.get("interaction")
                if not isinstance(interaction, dict):
                    raise WorkflowError(
                        "invalid_interaction", "interaction object required", status=422
                    )
                receipt = service.request_interaction(interaction=interaction, **common)
            elif signal_type == "control_acknowledged":
                receipt = service.acknowledge_control(
                    control=str(body.get("control") or ""), **common
                )
            else:
                raise WorkflowError("invalid_signal", "unsupported signal_type", status=422)
            return self._json(receipt, status=202 if receipt.disposition == "accepted" else 200)
        except Exception as exc:
            return self._error(exc)

    async def get_checkpoint(self, request: web.Request) -> web.Response:
        try:
            checkpoint = self._service(request.match_info["project_id"]).repository.load_checkpoint(
                request.match_info["run_id"], request.match_info["checkpoint_id"]
            )
            return self._json(checkpoint)
        except Exception as exc:
            return self._error(exc)

    async def list_events(self, request: web.Request) -> web.Response:
        try:
            after = max(int(request.query.get("after", "0")), 0)
            limit = min(max(int(request.query.get("limit", "100")), 1), 500)
            service = self._service(request.match_info["project_id"])
            events = service.list_events(after=after, limit=limit)
            workflow = service.repository.load_workflow()
            return web.json_response(
                {
                    "events": [event.model_dump(mode="json") for event in events],
                    "next_after": events[-1].project_sequence if events else after,
                    "has_more": bool(events and events[-1].project_sequence < workflow.last_project_sequence),
                    "watermark": workflow.last_project_sequence,
                }
            )
        except (TypeError, ValueError):
            return web.json_response(
                {"error": {"code": "invalid_query", "message": "after and limit must be integers", "details": {}}},
                status=400,
            )
        except Exception as exc:
            return self._error(exc)

    async def list_artifacts(self, request: web.Request) -> web.Response:
        try:
            artifacts = self._service(request.match_info["project_id"]).repository.list_artifacts()
            return web.json_response({"artifacts": [item.model_dump(mode="json") for item in artifacts]})
        except Exception as exc:
            return self._error(exc)

    async def import_artifact(self, request: web.Request) -> web.Response:
        try:
            body = await self._body(request)
            artifact, event = self._service(request.match_info["project_id"]).import_artifact(
                command_id=str(body.get("command_id") or ""),
                source_path=str(body.get("source_path") or ""),
                artifact_type=str(body.get("artifact_type") or "file"),
                label=body.get("label") if isinstance(body.get("label"), str) else None,
                media_type=str(body.get("media_type") or "application/octet-stream"),
                derived_from=body.get("derived_from") if isinstance(body.get("derived_from"), list) else None,
            )
            return web.json_response(
                {"artifact": artifact.model_dump(mode="json"), "project_sequence": event.project_sequence},
                status=201,
            )
        except Exception as exc:
            return self._error(exc)

    async def get_artifact(self, request: web.Request) -> web.Response:
        try:
            artifact = self._service(request.match_info["project_id"]).repository.load_artifact(
                request.match_info["artifact_id"]
            )
            return self._json(artifact)
        except Exception as exc:
            return self._error(exc)

    async def get_artifact_content(self, request: web.Request) -> web.StreamResponse:
        try:
            repository = self._service(request.match_info["project_id"]).repository
            artifact = repository.load_artifact(request.match_info["artifact_id"])
            path = repository.artifact_content_path(artifact)
            if is_deck_file(path):
                return deck_response(
                    path, edit=request.query.get("mode") == "edit", content_type=artifact.media_type,
                )
            return web.FileResponse(path, headers={"Content-Type": artifact.media_type})
        except Exception as exc:
            return self._error(exc)

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break
                    continue
                try:
                    data = message.json()
                except ValueError:
                    await ws.send_json(self._protocol_error("invalid_json", "valid JSON required"))
                    continue
                if not isinstance(data, dict) or data.get("message_type") != "subscribe":
                    await ws.send_json(self._protocol_error("invalid_message", "subscribe message required"))
                    continue
                await self._subscribe(ws, data)
        finally:
            self._remove_socket(ws)
        return ws

    async def _subscribe(self, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            await ws.send_json(self._protocol_error("project_id_required", "project_id required"))
            return
        try:
            after = int(data.get("after_project_sequence") or 0)
            service = self._service(project_id)
            workflow, _runs = service.snapshot()
        except Exception as exc:
            status, payload = error_payload(exc)
            payload["status"] = status
            await ws.send_json({"message_type": "protocol_error", **payload})
            return
        if after < 0 or after > workflow.last_project_sequence:
            await ws.send_json(
                {
                    "message_type": "snapshot_required",
                    "project_id": project_id,
                    "reason": "invalid_cursor",
                    "watermark": workflow.last_project_sequence,
                }
            )
            return
        subscription_id = str(data.get("subscription_id") or uuid4())
        existing = self._subscribers[project_id].pop(subscription_id, None)
        if existing:
            self._socket_subscriptions[existing.ws].discard((project_id, subscription_id))
        subscriber = _Subscriber(
            subscription_id=subscription_id,
            project_id=project_id,
            ws=ws,
            include_transient=bool(data.get("include_transient", True)),
            last_sent=after,
        )
        self._subscribers[project_id][subscription_id] = subscriber
        self._socket_subscriptions[ws].add((project_id, subscription_id))
        watermark = workflow.last_project_sequence
        replay_cursor = after
        while replay_cursor < watermark:
            replay = service.list_events(after=replay_cursor, limit=500)
            if not replay:
                await ws.send_json(
                    {
                        "message_type": "snapshot_required",
                        "project_id": project_id,
                        "reason": "replay_gap",
                        "watermark": watermark,
                    }
                )
                self._remove_subscription(project_id, subscription_id)
                return
            for event in replay:
                if event.project_sequence > watermark:
                    break
                await self._send_event(subscriber, event)
                replay_cursor = event.project_sequence
        subscriber.replaying = False
        for event in sorted(subscriber.buffered, key=lambda item: item.project_sequence):
            await self._send_event(subscriber, event)
        subscriber.buffered.clear()
        await ws.send_json(
            {
                "message_type": "subscribed",
                "subscription_id": subscription_id,
                "project_id": project_id,
                "watermark": workflow.last_project_sequence,
            }
        )

    async def _broadcast(self, event: WorkflowEvent) -> None:
        for subscriber in list(self._subscribers.get(event.project_id, {}).values()):
            if subscriber.ws.closed:
                self._remove_subscription(event.project_id, subscriber.subscription_id)
                continue
            if subscriber.replaying:
                subscriber.buffered.append(event)
                continue
            try:
                await self._send_event(subscriber, event)
            except (ConnectionError, RuntimeError):
                self._remove_subscription(event.project_id, subscriber.subscription_id)

    async def broadcast_activity(self, project_id: str, run_id: str, summary: str) -> None:
        """Publish coalescible, non-durable current-operation text."""
        compact = " ".join(summary.split())[:240]
        payload = {
            "message_type": "activity",
            "workflow_contract_version": 1,
            "project_id": project_id,
            "run_id": run_id,
            "summary": compact,
        }
        for subscriber in list(self._subscribers.get(project_id, {}).values()):
            if subscriber.include_transient and not subscriber.ws.closed:
                try:
                    await subscriber.ws.send_json(payload)
                except (ConnectionError, RuntimeError):
                    self._remove_subscription(project_id, subscriber.subscription_id)

    async def _send_event(self, subscriber: _Subscriber, event: WorkflowEvent) -> None:
        async with subscriber.lock:
            if event.project_sequence <= subscriber.last_sent:
                return
            await subscriber.ws.send_json(event.model_dump(mode="json"))
            subscriber.last_sent = event.project_sequence

    def _remove_subscription(self, project_id: str, subscription_id: str) -> None:
        subscriber = self._subscribers.get(project_id, {}).pop(subscription_id, None)
        if subscriber:
            self._socket_subscriptions[subscriber.ws].discard((project_id, subscription_id))
        if not self._subscribers.get(project_id):
            self._subscribers.pop(project_id, None)

    def _remove_socket(self, ws: web.WebSocketResponse) -> None:
        for project_id, subscription_id in list(self._socket_subscriptions.pop(ws, set())):
            self._remove_subscription(project_id, subscription_id)

    async def close(self) -> None:
        sockets = list(self._socket_subscriptions)
        self._subscribers.clear()
        self._socket_subscriptions.clear()
        for ws in sockets:
            await ws.close(code=1001, message=b"engine stopping")

    @staticmethod
    def _protocol_error(code: str, message: str) -> dict[str, Any]:
        return {"message_type": "protocol_error", "error": {"code": code, "message": message, "details": {}}}

    @staticmethod
    def _error(exc: Exception) -> web.Response:
        if isinstance(exc, FileNotFoundError):
            return web.json_response(
                {"error": {"code": "not_found", "message": str(exc), "details": {}}},
                status=404,
            )
        status, payload = error_payload(exc)
        return web.json_response(payload, status=status)
