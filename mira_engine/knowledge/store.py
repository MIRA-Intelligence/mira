"""On-disk knowledge bases: one directory and one SQLite index per base.

Layout under ``<MIRA_HOME>/knowledge/<kb_id>/``::

    kb.json            name, description, timestamps
    sources/<src_id>/  original uploads, extracted archives, git clones
    text/<doc_id>.txt  normalized text that chunks and ``kb_read`` refer to
    index.sqlite3      sources, documents, chunks and the chunks_fts table
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from mira_engine.utils.locks import locked_write_text

from .models import Chunk, Document, KnowledgeBase, SearchHit, Source
from .tokenize import fts_query, index_terms

SCHEMA_VERSION = 1
_KB_ID = re.compile(r"^kb_[0-9a-f]{12}$")
_SOURCE_COLUMNS = (
    "id", "kind", "title", "origin", "status", "error", "content_hash",
    "doc_count", "chunk_count", "meta", "created_at", "updated_at",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class KnowledgeNotFoundError(LookupError):
    pass


class KBIndex:
    """SQLite index of one knowledge base."""

    def __init__(self, kb_dir: Path):
        self.dir = kb_dir
        self.path = kb_dir / "index.sqlite3"
        self.text_dir = kb_dir / "text"
        self.sources_dir = kb_dir / "sources"

    def _connect(self) -> sqlite3.Connection:
        self.dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            with db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sources (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                        origin TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                        content_hash TEXT NOT NULL DEFAULT '', doc_count INTEGER NOT NULL DEFAULT 0,
                        chunk_count INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS documents (
                        id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                        path TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                        line_count INTEGER NOT NULL, page_starts TEXT);
                    CREATE INDEX IF NOT EXISTS documents_source ON documents(source_id);
                    CREATE TABLE IF NOT EXISTS chunks (
                        id INTEGER PRIMARY KEY,
                        doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                        start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                        page INTEGER, heading TEXT NOT NULL DEFAULT '', text TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                        terms, tokenize='unicode61 remove_diacritics 2');
                    """
                )
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return db

    # ── sources ──────────────────────────────────────────────────────

    @staticmethod
    def _source(row: sqlite3.Row) -> Source:
        data = dict(row)
        data["meta"] = json.loads(data.get("meta") or "{}")
        return Source(**data)

    def add_source(self, source: Source) -> Source:
        source.created_at = source.updated_at = now_iso()
        values = source.to_dict()
        values["meta"] = json.dumps(values["meta"], ensure_ascii=False)
        with closing(self._connect()) as db, db:
            db.execute(
                f"INSERT INTO sources ({','.join(_SOURCE_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(_SOURCE_COLUMNS))})",
                [values[c] for c in _SOURCE_COLUMNS],
            )
        return source

    def list_sources(self) -> list[Source]:
        if not self.path.exists():
            return []
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM sources ORDER BY rowid").fetchall()
        return [self._source(r) for r in rows]

    def get_source(self, source_id: str) -> Source:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise KnowledgeNotFoundError(f"source not found: {source_id}")
        return self._source(row)

    def update_source(self, source_id: str, **fields: Any) -> None:
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        assignments = ",".join(f"{k}=?" for k in fields if k in _SOURCE_COLUMNS)
        with closing(self._connect()) as db, db:
            db.execute(
                f"UPDATE sources SET {assignments} WHERE id=?",
                [v for k, v in fields.items() if k in _SOURCE_COLUMNS] + [source_id],
            )

    def source_dir(self, source_id: str) -> Path:
        return self.sources_dir / source_id

    def _delete_docs(self, db: sqlite3.Connection, source_id: str) -> list[str]:
        doc_ids = [r[0] for r in db.execute("SELECT id FROM documents WHERE source_id=?", (source_id,))]
        db.execute(
            "DELETE FROM chunks_fts WHERE rowid IN (SELECT c.id FROM chunks c "
            "JOIN documents d ON d.id=c.doc_id WHERE d.source_id=?)",
            (source_id,),
        )
        db.execute("DELETE FROM documents WHERE source_id=?", (source_id,))
        return doc_ids

    def _remove_text(self, doc_ids: Iterable[str]) -> None:
        for doc_id in doc_ids:
            (self.text_dir / f"{doc_id}.txt").unlink(missing_ok=True)

    def delete_source(self, source_id: str) -> None:
        with closing(self._connect()) as db, db:
            doc_ids = self._delete_docs(db, source_id)
            deleted = db.execute("DELETE FROM sources WHERE id=?", (source_id,)).rowcount
        if not deleted:
            raise KnowledgeNotFoundError(f"source not found: {source_id}")
        self._remove_text(doc_ids)
        shutil.rmtree(self.source_dir(source_id), ignore_errors=True)

    def replace_documents(
        self,
        source_id: str,
        documents: list[tuple[Document, list[Chunk]]],
    ) -> tuple[int, int]:
        """Atomically swap a source's documents and chunks; returns (docs, chunks)."""
        self.text_dir.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[str, Document, list[Chunk]]] = []
        for doc, chunks in documents:
            doc_id = new_id("doc")
            (self.text_dir / f"{doc_id}.txt").write_text(doc.text, encoding="utf-8")
            staged.append((doc_id, doc, chunks))
        chunk_count = 0
        try:
            with closing(self._connect()) as db, db:
                old_ids = self._delete_docs(db, source_id)
                for doc_id, doc, chunks in staged:
                    db.execute(
                        "INSERT INTO documents (id, source_id, path, title, url, line_count, page_starts) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (doc_id, source_id, doc.path, doc.title, doc.url, len(doc.text.splitlines()),
                         json.dumps(doc.page_starts) if doc.page_starts else None),
                    )
                    for chunk in chunks:
                        cur = db.execute(
                            "INSERT INTO chunks (doc_id, start_line, end_line, page, heading, text) "
                            "VALUES (?,?,?,?,?,?)",
                            (doc_id, chunk.start_line, chunk.end_line, chunk.page, chunk.heading, chunk.text),
                        )
                        # Headings and paths are searchable too: "auth" should find src/auth.py.
                        terms = index_terms(f"{doc.title} {doc.path} {chunk.heading}\n{chunk.text}")
                        db.execute("INSERT INTO chunks_fts (rowid, terms) VALUES (?, ?)", (cur.lastrowid, terms))
                        chunk_count += 1
        except BaseException:
            self._remove_text(doc_id for doc_id, _, _ in staged)
            raise
        self._remove_text(old_ids)
        return len(staged), chunk_count

    # ── reading ──────────────────────────────────────────────────────

    def search(self, query: str, top_k: int, kb: KnowledgeBase) -> list[SearchHit]:
        match = fts_query(query)
        if not match or not self.path.exists():
            return []
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT c.id, c.start_line, c.end_line, c.page, c.heading, c.text,
                       d.id AS doc_id, d.path, d.url, s.id AS source_id, s.title AS source_title,
                       bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN documents d ON d.id = c.doc_id
                JOIN sources s ON s.id = d.source_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (match, top_k),
            ).fetchall()
        return [
            SearchHit(
                kb_id=kb.id, kb_name=kb.name, source_id=r["source_id"], source_title=r["source_title"],
                doc_id=r["doc_id"], path=r["path"], url=r["url"], start_line=r["start_line"],
                end_line=r["end_line"], page=r["page"], heading=r["heading"], text=r["text"],
                score=-float(r["rank"]),
            )
            for r in rows
        ]

    def get_document(self, doc_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT d.*, s.title AS source_title FROM documents d "
                "JOIN sources s ON s.id=d.source_id WHERE d.id=?",
                (doc_id,),
            ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError(f"document not found: {doc_id}")
        data = dict(row)
        data["page_starts"] = json.loads(data["page_starts"]) if data["page_starts"] else None
        return data

    def list_documents(self, source_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, path, title, url, line_count FROM documents WHERE source_id=? ORDER BY path",
                (source_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def read_lines(self, doc_id: str, start: int, end: int) -> list[str]:
        path = self.text_dir / f"{doc_id}.txt"
        if not path.is_file():
            raise KnowledgeNotFoundError(f"document not found: {doc_id}")
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[max(start, 1) - 1 : end]

    def stats(self) -> dict[str, int]:
        if not self.path.exists():
            return {"sources": 0, "documents": 0, "chunks": 0}
        with closing(self._connect()) as db:
            return {
                "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "documents": db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                "chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            }


class KnowledgeStore:
    """All knowledge bases of one user (one ``MIRA_HOME``)."""

    def __init__(self, root: Path | None = None):
        self._root = root

    @property
    def root(self) -> Path:
        if self._root is None:
            from mira_engine.config.paths import get_knowledge_dir

            self._root = get_knowledge_dir()
        return self._root

    def kb_dir(self, kb_id: str) -> Path:
        if not _KB_ID.match(kb_id or ""):
            raise KnowledgeNotFoundError(f"knowledge base not found: {kb_id}")
        return self.root / kb_id

    def index(self, kb_id: str) -> KBIndex:
        self.get(kb_id)
        return KBIndex(self.kb_dir(kb_id))

    def _meta_path(self, kb_id: str) -> Path:
        return self.kb_dir(kb_id) / "kb.json"

    def _write(self, kb: KnowledgeBase) -> None:
        path = self._meta_path(kb.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_write_text(path, json.dumps(kb.to_dict(), ensure_ascii=False, indent=2))

    def get(self, kb_id: str) -> KnowledgeBase:
        path = self._meta_path(kb_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeNotFoundError(f"knowledge base not found: {kb_id}") from exc
        fields = KnowledgeBase.__dataclass_fields__
        return KnowledgeBase(**{k: v for k, v in data.items() if k in fields})

    def list(self) -> list[KnowledgeBase]:
        if not self.root.is_dir():
            return []
        out: list[KnowledgeBase] = []
        for child in sorted(self.root.iterdir()):
            if _KB_ID.match(child.name):
                try:
                    out.append(self.get(child.name))
                except KnowledgeNotFoundError:
                    continue
        return sorted(out, key=lambda kb: kb.created_at)

    def create(self, name: str, description: str = "") -> KnowledgeBase:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        now = now_iso()
        kb = KnowledgeBase(id=new_id("kb"), name=name, description=description.strip(), created_at=now, updated_at=now)
        self._write(kb)
        return kb

    def update(self, kb_id: str, *, name: str | None = None, description: str | None = None) -> KnowledgeBase:
        kb = self.get(kb_id)
        if name is not None:
            if not name.strip():
                raise ValueError("name is required")
            kb.name = name.strip()
        if description is not None:
            kb.description = description.strip()
        kb.updated_at = now_iso()
        self._write(kb)
        return kb

    def delete(self, kb_id: str) -> None:
        self.get(kb_id)
        shutil.rmtree(self.kb_dir(kb_id))

    def search(self, kb_ids: Iterable[str], query: str, top_k: int = 8) -> list[SearchHit]:
        """Search several bases; bm25 scores are comparable enough to merge directly."""
        hits: list[SearchHit] = []
        for kb_id in dict.fromkeys(kb_ids):
            try:
                kb = self.get(kb_id)
            except KnowledgeNotFoundError:
                continue
            hits.extend(KBIndex(self.kb_dir(kb_id)).search(query, top_k, kb))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def summary(self, kb_id: str) -> dict[str, Any]:
        kb = self.get(kb_id)
        return {**kb.to_dict(), **KBIndex(self.kb_dir(kb_id)).stats()}
