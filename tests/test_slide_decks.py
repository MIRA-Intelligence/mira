from __future__ import annotations

import json
import re

from mira_engine.utils.slide_decks import editable_deck, is_deck

DECK = (
    '<html><body><script type="application/bento+json" id="bento-doc">\n'
    '{"format": "bento/slides", "readonly": true, "title": "a \\u003c/script> b"}\n'
    "</script><script>boot()</script></body></html>"
)


def _doc(html: str) -> dict:
    block = re.search(r'id="bento-doc">(.*?)</script>', html, re.DOTALL)
    assert block is not None
    return json.loads(block.group(1))


def test_editable_deck_drops_readonly_and_keeps_the_rest() -> None:
    html = editable_deck(DECK)
    assert html is not None
    doc = _doc(html)
    assert "readonly" not in doc
    assert doc["title"] == "a </script> b"
    assert "</script> b" not in html  # "<" stays escaped inside the JSON block
    assert html.endswith("</script><script>boot()</script></body></html>")


def test_editable_deck_rejects_other_html() -> None:
    assert editable_deck("<html><body>report</body></html>") is None
    assert editable_deck('<script type="application/bento+json" id="bento-doc">not json</script>') is None


def test_is_deck() -> None:
    assert is_deck("result/exports/slides.bento.html")
    assert is_deck("Deck.MIRA.HTML")
    assert not is_deck("report.html")
