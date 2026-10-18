"""Data types shared by knowledge base storage, ingest and search."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SourceKind = Literal["file", "archive", "url", "git", "dir"]
SourceStatus = Literal["pending", "processing", "ready", "failed"]
SOURCE_KINDS: tuple[str, ...] = ("file", "archive", "url", "git", "dir")


@dataclass
class KnowledgeBase:
    id: str
    name: str
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Source:
    id: str
    kind: str
    title: str
    origin: str  # file name, URL, git URL or local directory
    status: str = "pending"
    error: str = ""
    content_hash: str = ""
    doc_count: int = 0
    chunk_count: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Document:
    """Normalized text produced by an extractor; one source yields one or more."""

    path: str  # display path: file name, path inside a repo, or URL
    text: str
    title: str = ""
    # 1-based line on which each PDF page starts, so chunks can cite pages.
    page_starts: list[int] | None = None
    url: str = ""


@dataclass
class Chunk:
    text: str
    start_line: int
    end_line: int
    heading: str = ""
    page: int | None = None


@dataclass
class SearchHit:
    kb_id: str
    kb_name: str
    source_id: str
    source_title: str
    doc_id: str
    path: str
    url: str
    start_line: int
    end_line: int
    page: int | None
    heading: str
    text: str
    score: float

    def citation(self) -> str:
        where = f"p.{self.page}" if self.page else f"L{self.start_line}-{self.end_line}"
        return f"[{self.kb_name}/{self.path}#{where}]"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "citation": self.citation()}
