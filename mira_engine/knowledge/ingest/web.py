"""Fetch a web page (HTML, PDF or text) into a knowledge base document."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx

from mira_engine.agent.tools.web import MAX_REDIRECTS, USER_AGENT
from mira_engine.security.network import validate_url_target

from ..models import Document
from .documents import decode_text, extract_pdf, html_document


def validate_web_url(url: str) -> None:
    ok, reason = validate_url_target(url)
    if not ok:
        raise ValueError(reason)


async def _check_hop(request: httpx.Request) -> None:
    # Re-validate every redirect hop, not just the final URL.
    ok, reason = validate_url_target(str(request.url))
    if not ok:
        raise ValueError(f"blocked URL {request.url}: {reason}")


async def fetch_url(url: str, dest: Path, *, max_bytes: int, proxy: str | None = None) -> tuple[Document, str]:
    """Download ``url`` (keeping a copy in ``dest``); returns the document and final URL."""
    validate_web_url(url)
    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        timeout=60.0,
        proxy=proxy,
        event_hooks={"request": [_check_hop]},
    ) as client:
        async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
            response.raise_for_status()
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > max_bytes:
                    raise ValueError("page is too large")
            ctype = response.headers.get("content-type", "").lower()
            final_url = str(response.url)
    dest.mkdir(parents=True, exist_ok=True)
    display = final_url
    is_pdf = "application/pdf" in ctype or urlparse(final_url).path.lower().endswith(".pdf") or body[:5] == b"%PDF-"
    if is_pdf:
        pdf = dest / "page.pdf"
        pdf.write_bytes(body)
        doc = extract_pdf(pdf, display)
        doc.url = final_url
        return doc, final_url
    text = decode_text(bytes(body))
    if text is None:
        raise ValueError(f"unsupported content type: {ctype or 'unknown'}")
    (dest / "page.html").write_text(text, encoding="utf-8")
    if "html" in ctype or text.lstrip()[:256].lower().startswith(("<!doctype", "<html")):
        doc = html_document(text, display, url=final_url)
    else:
        doc = Document(path=display, text=text.strip(), url=final_url)
    if not doc.text.strip():
        raise ValueError("page has no readable text (it may need JavaScript to render)")
    doc.title = doc.title or final_url
    return doc, final_url
