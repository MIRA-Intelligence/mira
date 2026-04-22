"""Web tools: web_search and web_fetch."""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from mira_engine.agent.tools.base import Tool
from mira_engine.config.schema import WebSearchConfig
from mira_engine.security.network import validate_resolved_url, validate_url_target

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")


def _strip_tags(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Legacy helper retained for compatibility tests."""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, str(e)
    if parsed.scheme not in {"http", "https"}:
        return False, f"Only http/https allowed, got '{parsed.scheme or 'none'}'"
    if not parsed.netloc:
        return False, "Missing domain"
    return True, ""


class WebSearchTool(Tool):
    """Search the web via configured provider."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        proxy: str | None = None,
        config: WebSearchConfig | None = None,
    ):
        self.proxy = proxy
        self._strict_brave_no_key = config is None
        if config is not None:
            self.config = config
        else:
            self.config = WebSearchConfig(provider="brave", api_key=api_key or "", max_results=max_results)
        if api_key:
            self.config.api_key = api_key
        self._init_api_key = api_key or self.config.api_key

    @property
    def api_key(self) -> str:
        return self._init_api_key or self.config.api_key

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        provider = (self.config.provider or "brave").strip().lower()
        n = min(max(count or self.config.max_results, 1), 10)

        if provider in {"", "brave"}:
            if self.api_key:
                return await self._search_brave(query, n)
            if self._strict_brave_no_key:
                return "Error: Brave Search API key not configured"
            return await self._search_duckduckgo(query, n)
        if provider == "tavily":
            return await self._search_tavily(query, n)
        if provider == "searxng":
            if not self.config.base_url:
                return await self._search_duckduckgo(query, n)
            parsed = urlparse(self.config.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return "Error: Invalid SearXNG base_url"
            return await self._search_searxng(query, n)
        if provider == "jina":
            return await self._search_jina(query, n)
        if provider == "duckduckgo":
            return await self._search_duckduckgo(query, n)
        return f"Error: unknown provider '{provider}'"

    @staticmethod
    def _format_results(query: str, rows: list[tuple[str, str, str]]) -> str:
        if not rows:
            return f"No results for: {query}"
        lines = [f"Results for: {query}\n"]
        for i, (title, url, snippet) in enumerate(rows, 1):
            lines.append(f"{i}. {title}\n   {url}")
            if snippet:
                lines.append(f"   {snippet}")
        return "\n".join(lines)

    async def _search_brave(self, query: str, count: int) -> str:
        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=self.config.timeout) as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": count},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                )
                r.raise_for_status()
            results = r.json().get("web", {}).get("results", [])[:count]
            rows = [(it.get("title", ""), it.get("url", ""), it.get("description", "")) for it in results]
            return self._format_results(query, rows)
        except httpx.ProxyError as e:
            return f"Proxy error: {e}"
        except Exception as e:
            return f"Error: {e}"

    async def _search_tavily(self, query: str, count: int) -> str:
        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=self.config.timeout) as client:
                r = await client.post(
                    "https://api.tavily.com/search",
                    json={"query": query, "max_results": count},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                r.raise_for_status()
            results = r.json().get("results", [])[:count]
            rows = [(it.get("title", ""), it.get("url", ""), it.get("content", "")) for it in results]
            return self._format_results(query, rows)
        except Exception as e:
            return f"Error: {e}"

    async def _search_searxng(self, query: str, count: int) -> str:
        base = self.config.base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=self.config.timeout) as client:
                r = await client.get(
                    f"{base}/search",
                    params={"q": query, "format": "json", "count": count},
                )
                r.raise_for_status()
            results = r.json().get("results", [])[:count]
            rows = [(it.get("title", ""), it.get("url", ""), it.get("content", "")) for it in results]
            return self._format_results(query, rows)
        except Exception as e:
            return f"Error: {e}"

    async def _search_jina(self, query: str, count: int) -> str:
        encoded = quote(query, safe="")
        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=self.config.timeout) as client:
                r = await client.get(
                    f"https://s.jina.ai/{encoded}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                r.raise_for_status()
            results = r.json().get("data", [])[:count]
            rows = [(it.get("title", ""), it.get("url", ""), it.get("content", "")) for it in results]
            return self._format_results(query, rows)
        except httpx.HTTPStatusError as e:
            if getattr(e.response, "status_code", None) == 422:
                return await self._search_duckduckgo(query, count)
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {e}"

    async def _search_duckduckgo(self, query: str, count: int) -> str:
        def _run() -> list[dict[str, Any]]:
            try:
                from ddgs import DDGS
            except ImportError:
                raise RuntimeError(
                    "DuckDuckGo search requires 'ddgs'. Install dependencies and retry."
                ) from None
            client = DDGS()
            return list(client.text(query, max_results=count))

        try:
            rows_raw = await asyncio.wait_for(asyncio.to_thread(_run), timeout=float(self.config.timeout))
            rows = [(it.get("title", ""), it.get("href", ""), it.get("body", "")) for it in rows_raw[:count]]
            return self._format_results(query, rows)
        except Exception as e:
            return f"Error: {e}"


class WebFetchTool(Tool):
    """Fetch URL content with SSRF checks and untrusted marker."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100},
        },
        "required": ["url"],
    }

    def __init__(self, max_chars: int = 50000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy

    async def execute(
        self,
        url: str,
        extractMode: str = "markdown",
        maxChars: int | None = None,
        **kwargs: Any,
    ) -> str:
        max_chars = maxChars or self.max_chars
        ok, reason = _validate_url(url)
        if not ok:
            return json.dumps({"error": f"URL validation failed: {reason}", "url": url}, ensure_ascii=False)
        ok, reason = validate_url_target(url)
        if not ok:
            return json.dumps({"error": reason, "url": url}, ensure_ascii=False)

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0,
                proxy=self.proxy,
            ) as client:
                parsed = urlparse(url)
                should_stream = parsed.path.lower().endswith(_IMAGE_SUFFIXES)
                if should_stream:
                    async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as r:
                        r.raise_for_status()
                        blocked, blocked_reason = validate_resolved_url(str(r.url))
                        if not blocked:
                            return json.dumps({"error": f"Redirect blocked: {blocked_reason}", "url": url}, ensure_ascii=False)
                        content = await r.aread()
                        ctype = r.headers.get("content-type", "")
                        if "image/" in ctype:
                            return json.dumps({"error": "redirect blocked before returning image", "url": url}, ensure_ascii=False)
                        text = content.decode("utf-8", errors="replace")
                        final_url = str(r.url)
                        status = getattr(r, "status_code", 200)
                else:
                    r = await client.get(url, headers={"User-Agent": USER_AGENT})
                    r.raise_for_status()
                    blocked, blocked_reason = validate_resolved_url(str(r.url))
                    if not blocked:
                        return json.dumps({"error": f"Redirect blocked: {blocked_reason}", "url": url}, ensure_ascii=False)
                    ctype = r.headers.get("content-type", "")
                    if "application/json" in ctype:
                        text = json.dumps(r.json(), indent=2, ensure_ascii=False)
                        extractor = "json"
                    elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                        from readability import Document

                        doc = Document(r.text)
                        content = self._to_markdown(doc.summary()) if extractMode == "markdown" else _strip_tags(doc.summary())
                        text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                        extractor = "readability"
                    else:
                        text = r.text
                        extractor = "raw"
                    final_url = str(r.url)
                    status = r.status_code
                    truncated = len(text) > max_chars
                    if truncated:
                        text = text[:max_chars]
                    rendered_text = f"[External content - untrusted]\n\n{text}" if extractor == "readability" else text
                    return json.dumps(
                        {
                            "url": url,
                            "finalUrl": final_url,
                            "status": status,
                            "extractor": extractor,
                            "truncated": truncated,
                            "length": len(text),
                            "untrusted": True,
                            "text": rendered_text,
                        },
                        ensure_ascii=False,
                    )

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return json.dumps(
                {
                    "url": url,
                    "finalUrl": final_url,
                    "status": status,
                    "extractor": "raw",
                    "truncated": truncated,
                    "length": len(text),
                    "untrusted": True,
                    "text": text,
                },
                ensure_ascii=False,
            )
        except httpx.ProxyError as e:
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    def _to_markdown(self, html_text: str) -> str:
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            lambda m: f'[{_strip_tags(m[2])}]({m[1]})',
            html_text,
            flags=re.I,
        )
        text = re.sub(
            r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
            lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n',
            text,
            flags=re.I,
        )
        text = re.sub(r"<li[^>]*>([\s\S]*?)</li>", lambda m: f"\n- {_strip_tags(m[1])}", text, flags=re.I)
        text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
        text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
        return _normalize(_strip_tags(text))
