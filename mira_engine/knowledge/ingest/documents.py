"""Extract text from uploaded documents: PDF, Word, HTML, Markdown and plain text."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from ..models import Document

PROSE_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc", ".org", ".tex", ".csv", ".tsv"})
HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
DOCUMENT_SUFFIXES = PROSE_SUFFIXES | HTML_SUFFIXES | {".pdf", ".docx"}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def is_document(path: Path) -> bool:
    return path.suffix.lower() in DOCUMENT_SUFFIXES


def decode_text(data: bytes) -> str | None:
    """UTF-8 (or a confidently detected encoding) text, or None for binary data."""
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    import chardet

    guess = chardet.detect(data[:65536])
    if guess.get("encoding") and (guess.get("confidence") or 0) >= 0.7:
        try:
            return data.decode(guess["encoding"])
        except (LookupError, UnicodeDecodeError):
            return None
    return None


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_pdf(path: Path, display: str) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    lines: list[str] = []
    page_starts: list[int] = []
    for page in reader.pages:
        page_starts.append(len(lines) + 1)
        try:
            text = page.extract_text() or ""
        except Exception:  # one malformed page must not lose the whole file
            text = ""
        lines.extend(_clean(text).splitlines() or [""])
        lines.append("")
    title = ""
    try:
        title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        pass
    text = "\n".join(lines)
    if not text.strip():
        raise ValueError("no extractable text (scanned PDF? OCR is not supported yet)")
    return Document(path=display, text=text, title=title or path.stem, page_starts=page_starts)


def extract_docx(path: Path, display: str) -> Document:
    try:
        with zipfile.ZipFile(path) as zf:
            root = ElementTree.fromstring(zf.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ValueError("invalid .docx file") from exc
    paragraphs: list[str] = []
    for para in root.iter(f"{_W}p"):
        style = para.find(f"{_W}pPr/{_W}pStyle")
        text = "".join(t.text or "" for t in para.iter(f"{_W}t")).strip()
        if not text:
            continue
        level = re.search(r"(\d)$", style.get(f"{_W}val", "")) if style is not None else None
        is_heading = style is not None and "heading" in style.get(f"{_W}val", "").lower()
        paragraphs.append(f"{'#' * int(level.group(1) if level else 1)} {text}" if is_heading else text)
    return Document(path=display, text="\n\n".join(paragraphs), title=path.stem)


def html_document(html_text: str, display: str, url: str = "") -> Document:
    from readability import Document as Readable

    from mira_engine.agent.tools.web import html_to_markdown

    readable = Readable(html_text)
    title = (readable.title() or "").strip()
    body = html_to_markdown(readable.summary())
    text = f"# {title}\n\n{body}" if title and not body.startswith("# ") else body
    return Document(path=display, text=_clean(text), title=title, url=url)


def extract_text_file(path: Path, display: str) -> Document | None:
    text = decode_text(path.read_bytes())
    if text is None:
        return None
    if path.suffix.lower() in HTML_SUFFIXES:
        return html_document(text, display)
    return Document(path=display, text=_clean(text), title=path.stem)


def extract_document(path: Path, display: str | None = None) -> Document | None:
    """Extract one file; None when it is binary or an unsupported format."""
    display = display or path.name
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path, display)
    if suffix == ".docx":
        return extract_docx(path, display)
    return extract_text_file(path, display)
