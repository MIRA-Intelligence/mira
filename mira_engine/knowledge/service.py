"""Background ingest: sources are queued on creation and indexed one at a time."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any

from loguru import logger

from mira_engine.config.schema import KnowledgeConfig

from .chunker import chunk_document
from .ingest import IngestLimits, extract_source
from .models import Source
from .store import KnowledgeNotFoundError, KnowledgeStore, new_id


class KnowledgeService:
    def __init__(
        self,
        store: KnowledgeStore | None = None,
        config: Callable[[], KnowledgeConfig] | None = None,
        proxy: Callable[[], str | None] | None = None,
    ):
        self.store = store or KnowledgeStore()
        self._config = config or KnowledgeConfig
        self._proxy = proxy or (lambda: None)
        self._queue: asyncio.Queue[tuple[str, str]] | None = None
        self._worker: asyncio.Task | None = None

    @property
    def config(self) -> KnowledgeConfig:
        return self._config()

    def limits(self) -> IngestLimits:
        cfg = self.config
        return IngestLimits(
            max_upload_bytes=cfg.max_upload_mb * 1024 * 1024,
            max_code_files=cfg.max_code_files,
            max_file_bytes=cfg.max_file_kb * 1024,
        )

    # ── queue ────────────────────────────────────────────────────────

    def _ensure_worker(self) -> asyncio.Queue[tuple[str, str]]:
        if self._queue is None:
            self._queue = asyncio.Queue()
            # Sources interrupted by an engine restart are picked up again.
            for kb in self.store.list():
                for source in self.store.index(kb.id).list_sources():
                    if source.status in ("pending", "processing"):
                        self._queue.put_nowait((kb.id, source.id))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="knowledge-ingest")
        return self._queue

    def start(self) -> None:
        self._ensure_worker()

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None

    def submit(self, kb_id: str, source_id: str) -> None:
        self._ensure_worker().put_nowait((kb_id, source_id))

    async def drain(self) -> None:
        """Wait until every queued source has been processed (tests, CLI)."""
        if self._queue is not None:
            await self._queue.join()

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            kb_id, source_id = await self._queue.get()
            try:
                await self.process(kb_id, source_id)
            except Exception:
                logger.exception("Knowledge ingest crashed for {}/{}", kb_id, source_id)
            finally:
                self._queue.task_done()

    # ── sources ──────────────────────────────────────────────────────

    def create_source(self, kb_id: str, kind: str, title: str, origin: str, meta: dict[str, Any] | None = None) -> Source:
        index = self.store.index(kb_id)
        source = Source(id=new_id("src"), kind=kind, title=title, origin=origin, meta=dict(meta or {}))
        index.source_dir(source.id).mkdir(parents=True, exist_ok=True)
        return index.add_source(source)

    def refresh(self, kb_id: str, source_id: str) -> Source:
        index = self.store.index(kb_id)
        index.get_source(source_id)
        index.update_source(source_id, status="pending", error="")
        self.submit(kb_id, source_id)
        return index.get_source(source_id)

    async def process(self, kb_id: str, source_id: str) -> None:
        try:
            index = self.store.index(kb_id)
            source = index.get_source(source_id)
        except KnowledgeNotFoundError:
            return  # deleted while queued
        index.update_source(source_id, status="processing", error="")
        try:
            docs, meta_update = await extract_source(
                source, index.source_dir(source_id), self.limits(), proxy=self._proxy(),
            )
            meta = {**source.meta, **meta_update}
            title = meta.get("page_title") or source.title
            if not docs:
                raise ValueError("no indexable text found")
            digest = hashlib.sha256()
            for doc, _ in docs:
                digest.update(doc.path.encode("utf-8") + b"\0" + doc.text.encode("utf-8") + b"\0")
            content_hash = digest.hexdigest()
            if content_hash == source.content_hash and source.chunk_count:
                index.update_source(source_id, status="ready", title=title, meta=meta)
                return
            chunked = await asyncio.to_thread(
                lambda: [(doc, chunk_document(doc, code=is_code)) for doc, is_code in docs]
            )
            doc_count, chunk_count = await asyncio.to_thread(index.replace_documents, source_id, chunked)
            index.update_source(
                source_id, status="ready", title=title, content_hash=content_hash,
                doc_count=doc_count, chunk_count=chunk_count, meta=meta,
            )
            logger.info("Indexed knowledge source {}/{}: {} docs, {} chunks", kb_id, source_id, doc_count, chunk_count)
        except Exception as exc:
            logger.warning("Knowledge ingest failed for {}/{}: {}", kb_id, source_id, exc)
            try:
                index.update_source(source_id, status="failed", error=str(exc)[:500] or type(exc).__name__)
            except Exception:
                pass
