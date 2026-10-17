"""Turn text into space-separated terms for SQLite FTS5.

FTS5's ``unicode61`` tokenizer splits on whitespace and punctuation, so a run of
Chinese text would become a single token. CJK runs are segmented with jieba
before indexing; queries go through the same path so both sides agree.
Identifiers are also split on camelCase so ``getUserName`` matches ``user``.
"""

from __future__ import annotations

import logging
import re
import threading

_CJK = "㐀-䶿一-鿿豈-﫿"
_SEGMENT = re.compile(rf"([{_CJK}]+)|([^\W{_CJK}]+)")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
# Single CJK characters that carry no meaning on their own.
_CJK_STOP = frozenset("的了是在和与及或也就都而且被把对从为以之其这那个们吗呢吧啊")

_jieba_lock = threading.Lock()
_jieba = None


def _segmenter():
    global _jieba
    if _jieba is None:
        with _jieba_lock:
            if _jieba is None:
                import jieba

                jieba.setLogLevel(logging.WARNING)
                jieba.initialize()
                _jieba = jieba
    return _jieba


def _terms(text: str) -> list[str]:
    out: list[str] = []
    for cjk, word in _SEGMENT.findall(text):
        if cjk:
            out.extend(t for t in _segmenter().cut_for_search(cjk) if t.strip())
            continue
        out.append(word.lower())
        parts = [p.lower() for piece in word.split("_") for p in _CAMEL.findall(piece) if len(p) > 1]
        if len(parts) > 1:
            out.extend(parts)
    return out


def index_terms(text: str) -> str:
    """Terms to store in the FTS column for ``text``."""
    return " ".join(_terms(text))


def query_terms(query: str) -> list[str]:
    """Distinct search terms; single-character CJK stop words are dropped."""
    terms = list(dict.fromkeys(_terms(query)))
    meaningful = [t for t in terms if t not in _CJK_STOP]
    return meaningful or terms


def fts_query(query: str) -> str:
    """An FTS5 MATCH expression that ORs the query terms; empty when none."""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in query_terms(query))
