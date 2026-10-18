"""Serve MIRA Slides (bento) decks: sandboxed, and as an editable copy on request.

Decks are built read-only so the inline preview boots into the slideshow.
``?mode=edit`` on an artifact URL returns the same file with the ``readonly``
flag removed, so a browser tab opens the editor; the file on disk is unchanged.

Decks are agent-generated HTML opened on the engine origin with the gateway
token in the URL, so every deck response is CSP-sandboxed without
``allow-same-origin``: it runs in an opaque origin, cannot read engine or UI
storage, and the engine rejects its ``Origin: null`` API calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiohttp import web

DECK_SUFFIXES = (".bento.html", ".mira.html")
_DOC_BLOCK = re.compile(
    r'(<script type="application/bento\+json" id="bento-doc">)(.*?)(</script>)',
    re.DOTALL,
)
_SANDBOX = "sandbox allow-scripts allow-downloads allow-modals allow-popups allow-forms"
# The editor works offline; blocking network fetches keeps the URL's token in the tab.
_EDIT_CSP = f"{_SANDBOX}; connect-src data: blob:"


def is_deck(name: str) -> bool:
    return name.lower().endswith(DECK_SUFFIXES)


def editable_deck(html: str) -> str | None:
    """The deck with its ``readonly`` flag removed, or None if ``html`` is no deck."""
    match = _DOC_BLOCK.search(html)
    if match is None:
        return None
    try:
        doc = json.loads(match.group(2))
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    doc.pop("readonly", None)
    # Escape every "<" so the JSON can never contain a literal </script>.
    body = json.dumps(doc, ensure_ascii=False, indent=1).replace("<", "\\u003c")
    return f"{html[:match.start(2)]}\n{body}\n{html[match.end(2):]}"


def deck_response(path: Path, *, edit: bool, content_type: str | None = None) -> web.StreamResponse:
    """Serve a deck file sandboxed; with ``edit``, as an editable copy."""
    if not edit:
        headers = {"Content-Security-Policy": _SANDBOX}
        if content_type:
            headers["Content-Type"] = content_type
        return web.FileResponse(path, headers=headers)
    html = editable_deck(path.read_text(encoding="utf-8"))
    if html is None:
        return web.json_response({"error": "not an editable slide deck"}, status=400)
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Content-Security-Policy": _EDIT_CSP, "Cache-Control": "no-store"},
    )
