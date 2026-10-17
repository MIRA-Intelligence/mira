"""Split normalized documents into search chunks that keep line numbers.

Prose is grouped by paragraph under its markdown heading; code is cut into
fixed line windows. Every chunk remembers its 1-based line range so search
hits can be cited and expanded with ``kb_read``.
"""

from __future__ import annotations

import bisect
import re

from .models import Chunk, Document

MAX_CHARS = 1800  # roughly 500-800 tokens for mixed English and Chinese
OVERLAP_CHARS = 300
MIN_SECTION_CHARS = 200
CODE_LINES = 80
CODE_OVERLAP_LINES = 10

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _page_for(doc: Document, line: int) -> int | None:
    if not doc.page_starts:
        return None
    return max(bisect.bisect_right(doc.page_starts, line), 1)


def _pieces(lines: list[str]) -> list[tuple[int, int, str, str]]:
    """Paragraphs as (start, end, text, heading), split further when too long."""
    out: list[tuple[int, int, str, str]] = []
    headings: list[str] = []
    para: list[tuple[int, str]] = []

    def flush() -> None:
        if not para:
            return
        heading = " > ".join(headings)
        buf: list[tuple[int, str]] = []
        size = 0
        for no, text in para:
            while len(text) > MAX_CHARS:  # one huge line, e.g. PDF text without breaks
                if buf:
                    out.append((buf[0][0], buf[-1][0], "\n".join(t for _, t in buf), heading))
                    buf, size = [], 0
                out.append((no, no, text[:MAX_CHARS], heading))
                text = text[MAX_CHARS - OVERLAP_CHARS:]
            if buf and size + len(text) > MAX_CHARS:
                out.append((buf[0][0], buf[-1][0], "\n".join(t for _, t in buf), heading))
                buf, size = [], 0
            buf.append((no, text))
            size += len(text) + 1
        if buf:
            out.append((buf[0][0], buf[-1][0], "\n".join(t for _, t in buf), heading))
        para.clear()

    for no, line in enumerate(lines, 1):
        match = _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            del headings[level - 1:]
            headings.extend([""] * (level - 1 - len(headings)))
            headings.append(match.group(2))
            out.append((no, no, line, " > ".join(h for h in headings if h)))
        elif line.strip():
            para.append((no, line))
        else:
            flush()
    flush()
    return out


def chunk_prose(doc: Document) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: list[tuple[int, int, str, str]] = []
    size = 0

    def emit() -> None:
        text = "\n\n".join(p[2] for p in cur).strip()
        if text:
            chunks.append(Chunk(
                text=text, start_line=cur[0][0], end_line=cur[-1][1],
                heading=cur[-1][3], page=_page_for(doc, cur[0][0]),
            ))

    for piece in _pieces(doc.text.splitlines()):
        is_heading = bool(_HEADING.match(piece[2]))
        new_section = is_heading and size >= MIN_SECTION_CHARS
        # Never let a chunk span PDF pages, so every hit cites one page.
        new_section = new_section or bool(cur) and _page_for(doc, piece[0]) != _page_for(doc, cur[0][0])
        if cur and (new_section or size + len(piece[2]) > MAX_CHARS):
            emit()
            tail = cur[-1]
            # Carry a short trailing paragraph into the next chunk for context.
            cur = [tail] if not new_section and len(tail[2]) <= OVERLAP_CHARS else []
            size = sum(len(p[2]) for p in cur)
        cur.append(piece)
        size += len(piece[2]) + 2
    if cur:
        emit()
    return chunks


def chunk_code(doc: Document) -> list[Chunk]:
    lines = doc.text.splitlines()
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines) and end - start < CODE_LINES and (end == start or size + len(lines[end]) <= MAX_CHARS):
            size += len(lines[end]) + 1
            end += 1
        text = "\n".join(lines[start:end])[: MAX_CHARS * 2]
        if text.strip():
            chunks.append(Chunk(text=text, start_line=start + 1, end_line=end))
        if end >= len(lines):
            break
        start = max(end - CODE_OVERLAP_LINES, start + 1)
    return chunks


def chunk_document(doc: Document, *, code: bool) -> list[Chunk]:
    return chunk_code(doc) if code else chunk_prose(doc)
