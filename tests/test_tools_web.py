from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from medpilot.agent.tools import web as web_mod
from medpilot.agent.tools.web import WebFetchTool, WebSearchTool, _normalize, _strip_tags, _validate_url


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
        json_data: dict | None = None,
        url: str = "https://example.com/final",
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_data = json_data
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "bad",
                request=httpx.Request("GET", "https://example.com"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)


class _FakeAsyncClient:
    def __init__(self, *, response: _FakeResponse | None = None, error: Exception | None = None, **kwargs):
        self._response = response
        self._error = error
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        if self._error:
            raise self._error
        return self._response or _FakeResponse(text="")


def test_validate_url_and_text_helpers() -> None:
    assert _validate_url("https://example.com")[0] is True
    assert _validate_url("ftp://example.com")[0] is False
    assert "a b" == _normalize("a\t\tb")
    assert "Hello" == _strip_tags("<script>x</script><p>Hello</p>")


async def test_web_search_requires_api_key() -> None:
    tool = WebSearchTool(api_key="")
    result = await tool.execute("medpilot")
    assert "API key not configured" in result


async def test_web_search_success_and_no_results(monkeypatch) -> None:
    payload = {"web": {"results": [{"title": "A", "url": "https://a", "description": "d"}]}}
    response = _FakeResponse(headers={"content-type": "application/json"}, json_data=payload)
    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=response, **kwargs),
    )
    tool = WebSearchTool(api_key="k")
    output = await tool.execute("hello", count=1)
    assert "Results for: hello" in output
    assert "https://a" in output

    empty_response = _FakeResponse(headers={"content-type": "application/json"}, json_data={"web": {"results": []}})
    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=empty_response, **kwargs),
    )
    no_results = await tool.execute("none")
    assert no_results == "No results for: none"


async def test_web_search_proxy_and_generic_error(monkeypatch) -> None:
    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(error=httpx.ProxyError("proxy down"), **kwargs),
    )
    tool = WebSearchTool(api_key="k")
    assert "Proxy error" in await tool.execute("x")

    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(error=RuntimeError("boom"), **kwargs),
    )
    assert "Error: boom" in await tool.execute("x")


async def test_web_fetch_invalid_url() -> None:
    tool = WebFetchTool()
    data = json.loads(await tool.execute("file:///etc/passwd"))
    assert "URL validation failed" in data["error"]


async def test_web_fetch_json_html_and_raw(monkeypatch) -> None:
    tool = WebFetchTool(max_chars=200)

    json_resp = _FakeResponse(
        headers={"content-type": "application/json"},
        json_data={"ok": True},
    )
    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=json_resp, **kwargs),
    )
    payload = json.loads(await tool.execute("https://example.com/data"))
    assert payload["extractor"] == "json"
    assert '"ok": true' in payload["text"]

    class _Doc:
        def __init__(self, _html):
            pass

        def summary(self):
            return "<h1>T</h1><p>Hello <a href='https://x'>x</a></p>"

        def title(self):
            return "Demo"

    monkeypatch.setitem(__import__("sys").modules, "readability", SimpleNamespace(Document=_Doc))
    html_resp = _FakeResponse(headers={"content-type": "text/html"}, text="<html><body>x</body></html>")
    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=html_resp, **kwargs),
    )
    html_payload = json.loads(await tool.execute("https://example.com/page", extractMode="markdown"))
    assert html_payload["extractor"] == "readability"
    assert "Demo" in html_payload["text"]
    assert "[x](https://x)" in html_payload["text"]

    raw_resp = _FakeResponse(headers={"content-type": "text/plain"}, text="raw-data")
    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=raw_resp, **kwargs),
    )
    raw_payload = json.loads(await tool.execute("https://example.com/raw"))
    assert raw_payload["extractor"] == "raw"
    assert raw_payload["text"] == "raw-data"


async def test_web_fetch_proxy_and_generic_error(monkeypatch) -> None:
    tool = WebFetchTool()
    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(error=httpx.ProxyError("proxy"), **kwargs),
    )
    assert "Proxy error" in json.loads(await tool.execute("https://example.com"))["error"]

    monkeypatch.setattr(
        web_mod.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(error=RuntimeError("oops"), **kwargs),
    )
    assert json.loads(await tool.execute("https://example.com"))["error"] == "oops"
