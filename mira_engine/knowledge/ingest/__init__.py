"""Turn a knowledge source (file, archive, URL, git repo, directory) into documents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..models import Document, Source
from .code import extract_tree, extract_zip, git_clone
from .documents import extract_document
from .web import fetch_url


@dataclass
class IngestLimits:
    max_upload_bytes: int
    max_code_files: int
    max_file_bytes: int


async def extract_source(
    source: Source,
    source_dir: Path,
    limits: IngestLimits,
    *,
    proxy: str | None = None,
) -> tuple[list[tuple[Document, bool]], dict]:
    """Documents for ``source`` (flag: chunk as code) plus meta updates for the source."""
    tree = dict(max_files=limits.max_code_files, max_file_bytes=limits.max_file_bytes)
    if source.kind == "file":
        path = source_dir / source.meta["file"]
        doc = await asyncio.to_thread(extract_document, path, source.title)
        if doc is None or not doc.text.strip():
            raise ValueError("unsupported or empty file (binary content?)")
        return [(doc, False)], {}
    if source.kind == "archive":
        if source.meta.get("root"):  # refresh: the archive was extracted on first ingest
            root = source_dir / source.meta["root"]
            return await asyncio.to_thread(extract_tree, root, **tree), {}
        archive = source_dir / source.meta["file"]
        root = await asyncio.to_thread(
            extract_zip, archive, source_dir / "files",
            max_files=limits.max_code_files, max_total_bytes=limits.max_upload_bytes * 5,
        )
        archive.unlink(missing_ok=True)  # keep the extracted tree, not both copies
        meta = {"root": root.relative_to(source_dir).as_posix()}
        return await asyncio.to_thread(extract_tree, root, **tree), meta
    if source.kind == "dir":
        root = Path(source.origin)
        if not root.is_dir():
            raise ValueError(f"directory not found: {root}")
        return await asyncio.to_thread(extract_tree, root, **tree), {}
    if source.kind == "git":
        commit = await git_clone(source.origin, source_dir / "repo", source.meta.get("ref", ""))
        return await asyncio.to_thread(extract_tree, source_dir / "repo", **tree), {"commit": commit}
    if source.kind == "url":
        doc, final_url = await fetch_url(source.origin, source_dir, max_bytes=limits.max_upload_bytes, proxy=proxy)
        return [(doc, False)], {"final_url": final_url, "page_title": doc.title}
    raise ValueError(f"unknown source kind: {source.kind}")
