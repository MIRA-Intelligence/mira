from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from mira_engine.agent.tools.knowledge import KnowledgeReadTool, KnowledgeScope, KnowledgeSearchTool
from mira_engine.config.schema import KnowledgeConfig
from mira_engine.knowledge.api import KnowledgeHttpApi
from mira_engine.knowledge.service import KnowledgeService


@pytest.fixture
async def kb_client(store) -> AsyncIterator[tuple[TestClient, KnowledgeService, list]]:
    audit: list[tuple[str, dict]] = []
    service = KnowledgeService(store, config=lambda: KnowledgeConfig(max_upload_mb=1))
    app = web.Application()
    KnowledgeHttpApi(service, audit=lambda action, details: audit.append((action, details))).add_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, service, audit
    finally:
        await service.close()
        await client.close()


async def _create(client: TestClient, name: str = "Project docs") -> str:
    resp = await client.post("/api/knowledge", json={"name": name, "description": "specs"})
    assert resp.status == 201
    return (await resp.json())["id"]


async def test_upload_index_search_and_delete(kb_client, make_pdf) -> None:
    client, service, audit = kb_client
    kb_id = await _create(client)

    form = FormData()
    form.add_field("files", "# 部署指南\n\n使用 Docker 部署推理服务。".encode(), filename="guide.md")
    form.add_field("files", make_pdf(["Quarterly roadmap", "Latency budget is 40 ms"]), filename="plan.pdf")
    resp = await client.post(f"/api/knowledge/{kb_id}/sources", data=form)
    assert resp.status == 202
    sources = (await resp.json())["sources"]
    assert [s["status"] for s in sources] == ["pending", "pending"]

    await service.drain()
    detail = await (await client.get(f"/api/knowledge/{kb_id}")).json()
    assert detail["sources"][0]["status"] == "ready", detail
    assert detail["sources"][1]["status"] == "ready", detail
    assert detail["chunks"] >= 2

    hits = (await (await client.get(f"/api/knowledge/{kb_id}/search", params={"q": "部署"})).json())["hits"]
    assert hits[0]["path"] == "guide.md"
    hits = (await (await client.get(f"/api/knowledge/{kb_id}/search", params={"q": "latency budget"})).json())["hits"]
    assert hits[0]["page"] == 2 and hits[0]["citation"] == "[Project docs/plan.pdf#p.2]"

    pdf_id = sources[1]["id"]
    src = await (await client.get(f"/api/knowledge/{kb_id}/sources/{pdf_id}")).json()
    assert src["documents"][0]["path"] == "plan.pdf"
    doc_id = src["documents"][0]["id"]

    # Refreshing unchanged content keeps the existing chunks.
    resp = await client.post(f"/api/knowledge/{kb_id}/sources/{pdf_id}/refresh")
    assert resp.status == 202
    await service.drain()
    src = await (await client.get(f"/api/knowledge/{kb_id}/sources/{pdf_id}")).json()
    assert src["status"] == "ready" and src["documents"][0]["id"] == doc_id

    assert (await client.delete(f"/api/knowledge/{kb_id}/sources/{pdf_id}")).status == 200
    hits = (await (await client.get(f"/api/knowledge/{kb_id}/search", params={"q": "latency"})).json())["hits"]
    assert hits == []

    assert (await client.delete(f"/api/knowledge/{kb_id}")).status == 200
    assert (await client.get(f"/api/knowledge/{kb_id}")).status == 404
    assert [a for a, _ in audit] == [
        "knowledge_created", "knowledge_files_uploaded", "knowledge_source_deleted", "knowledge_deleted",
    ]


async def test_zip_and_directory_sources(kb_client, tmp_path: Path) -> None:
    client, service, _ = kb_client
    kb_id = await _create(client)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("svc/app/server.py", "def handleRequest(req):\n    return route(req)\n")
        zf.writestr("svc/README.md", "# Service\n\nThe gateway service.")
    form = FormData()
    form.add_field("files", buf.getvalue(), filename="svc.zip")
    assert (await client.post(f"/api/knowledge/{kb_id}/sources", data=form)).status == 202

    repo = tmp_path / "local-repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "util.go").write_text("func ParseConfig() {}\n", encoding="utf-8")
    resp = await client.post(f"/api/knowledge/{kb_id}/sources", json={"type": "dir", "path": str(repo)})
    assert resp.status == 202

    await service.drain()
    detail = await (await client.get(f"/api/knowledge/{kb_id}")).json()
    assert [s["status"] for s in detail["sources"]] == ["ready", "ready"], detail
    assert detail["sources"][0]["doc_count"] == 2

    hits = (await (await client.get(f"/api/knowledge/{kb_id}/search", params={"q": "handle request"})).json())["hits"]
    assert hits[0]["path"] == "app/server.py"
    hits = (await (await client.get(f"/api/knowledge/{kb_id}/search", params={"q": "parse config"})).json())["hits"]
    assert hits[0]["path"] == "lib/util.go"


async def test_source_validation(kb_client, tmp_path: Path) -> None:
    client, _, _ = kb_client
    kb_id = await _create(client)
    url = f"/api/knowledge/{kb_id}/sources"

    for body in (
        {"type": "url", "url": "http://127.0.0.1:8080/admin"},
        {"type": "url", "url": "file:///etc/passwd"},
        {"type": "git", "url": "http://example.com/repo.git"},
        {"type": "git", "url": "https://user:pw@example.com/repo.git"},
        {"type": "dir", "path": "relative/path"},
        {"type": "dir", "path": str(tmp_path / "missing")},
        {"type": "nope"},
    ):
        resp = await client.post(url, json=body)
        assert resp.status == 400, body

    # The Mira home (config and credentials) can never be indexed.
    from mira_engine.config.loader import get_home_dir

    get_home_dir().mkdir(parents=True, exist_ok=True)
    resp = await client.post(url, json={"type": "dir", "path": str(get_home_dir())})
    assert resp.status == 400

    form = FormData()
    form.add_field("files", b"x" * (1024 * 1024 + 1), filename="big.txt")
    assert (await client.post(url, data=form)).status == 413
    detail = await (await client.get(f"/api/knowledge/{kb_id}")).json()
    assert detail["sources"] == []

    assert (await client.post("/api/knowledge/kb_000000000000/sources", json={"type": "url"})).status == 404
    assert (await client.post("/api/knowledge", json={"name": " "})).status == 400


async def test_failed_source_reports_error(kb_client) -> None:
    client, service, _ = kb_client
    kb_id = await _create(client)
    form = FormData()
    form.add_field("files", b"\x00\x01binary", filename="blob.dat")
    await client.post(f"/api/knowledge/{kb_id}/sources", data=form)
    await service.drain()
    source = (await (await client.get(f"/api/knowledge/{kb_id}")).json())["sources"][0]
    assert source["status"] == "failed" and "unsupported" in source["error"]


async def test_tools_only_see_attached_knowledge_bases(kb_client, store, tmp_path: Path) -> None:
    client, service, _ = kb_client
    attached, other = await _create(client, "Attached"), await _create(client, "Other")
    for kb_id, text in ((attached, "向量数据库选型：使用 SQLite"), (other, "秘密 向量 内容")):
        form = FormData()
        form.add_field("files", text.encode(), filename="notes.md")
        await client.post(f"/api/knowledge/{kb_id}/sources", data=form)
    await service.drain()

    project = tmp_path / "proj"
    (project / ".mira").mkdir(parents=True)
    (project / ".mira" / "project.json").write_text(json.dumps({"knowledge_bases": [attached]}), encoding="utf-8")

    scope = KnowledgeScope(store)
    search, read = KnowledgeSearchTool(scope), KnowledgeReadTool(scope)

    prompt = scope.begin_turn({}, project)
    assert prompt and "Attached" in prompt and "Other" not in prompt
    result = await search.execute(query="向量")
    assert "Attached/notes.md" in result and "秘密" not in result
    assert "untrusted" in result
    doc_id = result.split("doc_id=")[1].split()[0]
    assert "向量数据库选型" in await read.execute(doc_id=doc_id)
    assert "None of those" in await search.execute(query="向量", kb_ids=[other])

    other_doc = store.search([other], "秘密", 1)[0].doc_id
    assert "not found" in await read.execute(doc_id=other_doc)

    # An explicit (empty) selection in the message overrides the project.
    assert scope.begin_turn({"selected_kb_ids": []}, project) is None
    assert "No knowledge bases" in await search.execute(query="向量")
    assert scope.begin_turn({"selected_kb_ids": [other]}, None)
    assert "秘密" in await search.execute(query="秘密")
