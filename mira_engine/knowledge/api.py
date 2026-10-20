"""aiohttp routes for managing knowledge bases (``/api/knowledge``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from .ingest.code import validate_git_url
from .ingest.web import validate_web_url
from .service import KnowledgeService
from .store import KnowledgeNotFoundError

AuditFn = Callable[[str, dict[str, Any]], None]


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _safe_filename(name: str) -> str | None:
    name = Path(name.replace("\\", "/")).name.strip()
    return name if name and name not in {".", ".."} else None


class KnowledgeHttpApi:
    def __init__(self, service: KnowledgeService, audit: AuditFn | None = None):
        self.service = service
        self.store = service.store
        self._audit = audit or (lambda action, details: None)

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/knowledge", self.list_kbs)
        app.router.add_post("/api/knowledge", self.create_kb)
        app.router.add_get("/api/knowledge/{kb_id}", self.get_kb)
        app.router.add_patch("/api/knowledge/{kb_id}", self.update_kb)
        app.router.add_delete("/api/knowledge/{kb_id}", self.delete_kb)
        app.router.add_get("/api/knowledge/{kb_id}/search", self.search)
        app.router.add_post("/api/knowledge/{kb_id}/sources", self.add_sources)
        app.router.add_get("/api/knowledge/{kb_id}/sources/{source_id}", self.get_source)
        app.router.add_delete("/api/knowledge/{kb_id}/sources/{source_id}", self.delete_source)
        app.router.add_post("/api/knowledge/{kb_id}/sources/{source_id}/refresh", self.refresh_source)

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError):
            raise web.HTTPBadRequest(text='{"error":"invalid JSON"}', content_type="application/json")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='{"error":"JSON body must be an object"}', content_type="application/json")
        return body

    def _detail(self, kb_id: str) -> dict[str, Any]:
        sources = self.store.index(kb_id).list_sources()
        return {**self.store.summary(kb_id), "sources": [s.to_dict() for s in sources]}

    # ── knowledge bases ──────────────────────────────────────────────

    async def list_kbs(self, _request: web.Request) -> web.Response:
        return web.json_response({"knowledge_bases": [self.store.summary(kb.id) for kb in self.store.list()]})

    async def create_kb(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        name, description = body.get("name"), body.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            return _error("name and description must be strings", 400)
        try:
            kb = self.store.create(name, description)
        except ValueError as exc:
            return _error(str(exc), 400)
        self._audit("knowledge_created", {"kb_id": kb.id, "name": kb.name})
        return web.json_response(self._detail(kb.id), status=201)

    async def get_kb(self, request: web.Request) -> web.Response:
        try:
            return web.json_response(self._detail(request.match_info["kb_id"]))
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)

    async def update_kb(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        fields = {k: body[k] for k in ("name", "description") if k in body}
        if not fields or not all(isinstance(v, str) for v in fields.values()):
            return _error("name or description (string) required", 400)
        try:
            self.store.update(request.match_info["kb_id"], **fields)
            return web.json_response(self._detail(request.match_info["kb_id"]))
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)
        except ValueError as exc:
            return _error(str(exc), 400)

    async def delete_kb(self, request: web.Request) -> web.Response:
        kb_id = request.match_info["kb_id"]
        try:
            self.store.delete(kb_id)
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)
        self._audit("knowledge_deleted", {"kb_id": kb_id})
        return web.json_response({"deleted": kb_id})

    async def search(self, request: web.Request) -> web.Response:
        kb_id = request.match_info["kb_id"]
        query = (request.query.get("q") or "").strip()
        if not query:
            return _error("q is required", 400)
        try:
            top_k = min(max(int(request.query.get("top_k", "8")), 1), 50)
            self.store.get(kb_id)
        except ValueError:
            return _error("top_k must be an integer", 400)
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)
        hits = self.store.search([kb_id], query, top_k)
        return web.json_response({"query": query, "hits": [h.to_dict() for h in hits]})

    # ── sources ──────────────────────────────────────────────────────

    async def add_sources(self, request: web.Request) -> web.Response:
        kb_id = request.match_info["kb_id"]
        try:
            self.store.get(kb_id)
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)
        if request.content_type.startswith("multipart/"):
            return await self._upload(request, kb_id)
        body = await self._json(request)
        kind = body.get("type")
        try:
            if kind == "url":
                url = str(body.get("url") or "").strip()
                validate_web_url(url)
                source = self.service.create_source(kb_id, "url", url, url)
            elif kind == "git":
                url = str(body.get("url") or "").strip()
                ref = str(body.get("ref") or "").strip()
                validate_git_url(url)
                title = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1] or url
                source = self.service.create_source(kb_id, "git", title + (f"@{ref}" if ref else ""), url, {"ref": ref})
            elif kind == "dir":
                root = self._validate_dir(str(body.get("path") or ""))
                source = self.service.create_source(kb_id, "dir", root.name or str(root), str(root))
            else:
                return _error("type must be one of: url, git, dir (or upload files as multipart)", 400)
        except ValueError as exc:
            return _error(str(exc), 400)
        self.service.submit(kb_id, source.id)
        self._audit("knowledge_source_added", {"kb_id": kb_id, "source_id": source.id, "kind": kind, "origin": source.origin})
        return web.json_response({"sources": [source.to_dict()]}, status=202)

    @staticmethod
    def _validate_dir(raw: str) -> Path:
        from mira_engine.config.loader import get_home_dir

        if not raw.strip():
            raise ValueError("path is required")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        root = path.resolve()
        if not root.is_dir():
            raise ValueError(f"directory not found: {raw}")
        home = get_home_dir().resolve()
        # The Mira home holds config and credentials; never index it.
        if root == home or root.is_relative_to(home) or home.is_relative_to(root):
            raise ValueError("cannot index a directory that contains the Mira home")
        return root

    async def _upload(self, request: web.Request, kb_id: str) -> web.Response:
        max_bytes = self.service.limits().max_upload_bytes
        index = self.store.index(kb_id)
        created = []
        try:
            reader = await request.multipart()
        except Exception:
            return _error("expected multipart/form-data", 400)
        while (part := await reader.next()) is not None:
            filename = _safe_filename(part.filename or "") if part.name == "files" else None
            if filename is None:
                await part.release()
                continue
            kind = "archive" if filename.lower().endswith(".zip") else "file"
            source = self.service.create_source(kb_id, kind, filename, filename, {"file": filename})
            dest = index.source_dir(source.id) / filename
            size = 0
            try:
                with dest.open("wb") as fh:
                    while chunk := await part.read_chunk():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError(f"{filename} exceeds {max_bytes // (1024 * 1024)} MB")
                        fh.write(chunk)
            except (ValueError, OSError) as exc:
                index.delete_source(source.id)
                for done in created:
                    index.delete_source(done.id)
                logger.warning("Knowledge upload failed: {}", exc)
                return _error(str(exc), 413 if isinstance(exc, ValueError) else 500)
            index.update_source(source.id, meta={"file": filename, "size": size})
            created.append(index.get_source(source.id))
        if not created:
            return _error("no files uploaded (use form field 'files')", 400)
        for source in created:
            self.service.submit(kb_id, source.id)
        self._audit("knowledge_files_uploaded", {"kb_id": kb_id, "files": [s.title for s in created]})
        return web.json_response({"sources": [s.to_dict() for s in created]}, status=202)

    async def get_source(self, request: web.Request) -> web.Response:
        try:
            index = self.store.index(request.match_info["kb_id"])
            source = index.get_source(request.match_info["source_id"])
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)
        return web.json_response({**source.to_dict(), "documents": index.list_documents(source.id)})

    async def delete_source(self, request: web.Request) -> web.Response:
        kb_id, source_id = request.match_info["kb_id"], request.match_info["source_id"]
        try:
            self.store.index(kb_id).delete_source(source_id)
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)
        self._audit("knowledge_source_deleted", {"kb_id": kb_id, "source_id": source_id})
        return web.json_response({"deleted": source_id})

    async def refresh_source(self, request: web.Request) -> web.Response:
        try:
            source = self.service.refresh(request.match_info["kb_id"], request.match_info["source_id"])
        except KnowledgeNotFoundError as exc:
            return _error(str(exc), 404)
        return web.json_response(source.to_dict(), status=202)
