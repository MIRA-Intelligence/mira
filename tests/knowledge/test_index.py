from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from mira_engine.knowledge.chunker import MAX_CHARS, chunk_code, chunk_prose
from mira_engine.knowledge.ingest.code import extract_tree, extract_zip
from mira_engine.knowledge.ingest.documents import extract_document
from mira_engine.knowledge.models import Document
from mira_engine.knowledge.store import KnowledgeNotFoundError
from mira_engine.knowledge.tokenize import fts_query, index_terms


def test_chinese_is_segmented_into_words() -> None:
    terms = index_terms("大语言模型的推理优化").split()
    assert {"模型", "推理", "优化"} <= set(terms)
    # Two-character words survive as standalone query terms.
    assert fts_query("模型怎么部署？") == '"模型" OR "怎么" OR "部署"'


def test_identifiers_are_split() -> None:
    terms = index_terms("def getUserName(parse_HTTPResponse): v2").split()
    assert {"getusername", "get", "user", "name", "http", "response"} <= set(terms)
    assert "v" not in terms


def test_prose_chunks_track_headings_lines_and_pages() -> None:
    body = "\n\n".join(f"段落 {i} " + "内容" * 200 for i in range(6))
    text = f"# 设计\n\n## 检索\n\n{body}\n"
    doc = Document(path="d.md", text=text, page_starts=[1, 10])
    chunks = chunk_prose(doc)
    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHARS * 2 for c in chunks)
    assert chunks[0].heading == "设计 > 检索"
    assert chunks[0].start_line == 1 and chunks[0].page == 1
    assert chunks[-1].page == 2
    lines = text.splitlines()
    for c in chunks:
        assert lines[c.end_line - 1].strip()  # ranges end on real content


def test_huge_single_line_is_split() -> None:
    chunks = chunk_prose(Document(path="x.txt", text="字" * (MAX_CHARS * 3)))
    assert len(chunks) >= 3
    assert all(c.start_line == 1 for c in chunks)


def test_code_chunks_overlap_and_keep_line_numbers() -> None:
    text = "\n".join(f"line_{i} = {i}" for i in range(1, 201))
    chunks = chunk_code(Document(path="a.py", text=text))
    assert chunks[0].start_line == 1 and chunks[0].end_line == 80
    assert chunks[1].start_line == 71
    assert chunks[-1].end_line == 200


def test_pdf_extraction_records_pages(tmp_path: Path, make_pdf) -> None:
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(make_pdf(["Alpha introduction", "Bravo retrieval details"]))
    doc = extract_document(pdf)
    assert doc is not None
    assert "Bravo retrieval details" in doc.text
    assert doc.page_starts == [1, 3]


def test_search_ranks_and_cites(store) -> None:
    kb = store.create("Docs", "project docs")
    index = store.index(kb.id)
    from mira_engine.knowledge.models import Source

    src = index.add_source(Source(id="src_1", kind="file", title="guide.md", origin="guide.md"))
    docs = [
        Document(path="guide.md", text="# 部署\n\n使用 Docker 部署推理服务。\n\n# 训练\n\n训练需要 GPU。"),
        Document(path="src/auth.py", text="def getUserName(token):\n    return decode(token)\n"),
    ]
    from mira_engine.knowledge.chunker import chunk_document

    count = index.replace_documents(src.id, [(d, chunk_document(d, code=d.path.endswith(".py"))) for d in docs])
    assert count[0] == 2

    hits = store.search([kb.id], "如何部署模型", 5)
    assert hits and hits[0].path == "guide.md" and "Docker" in hits[0].text
    assert hits[0].citation().startswith("[Docs/guide.md#L")

    assert store.search([kb.id], "user name", 5)[0].path == "src/auth.py"
    assert store.search([kb.id], "auth", 5)[0].path == "src/auth.py"  # path is indexed
    assert store.search([kb.id], "完全无关的词汇", 5) == []

    lines = index.read_lines(hits[0].doc_id, 1, 2)
    assert lines[0] == "# 部署"

    index.delete_source(src.id)
    assert store.search([kb.id], "部署", 5) == []
    assert not list((index.dir / "text").iterdir())
    with pytest.raises(KnowledgeNotFoundError):
        index.get_source(src.id)


def test_reindex_replaces_old_chunks(store) -> None:
    from mira_engine.knowledge.models import Source

    kb = store.create("KB")
    index = store.index(kb.id)
    index.add_source(Source(id="src_1", kind="file", title="a", origin="a"))
    old = Document(path="a.md", text="obsolete phrase")
    index.replace_documents("src_1", [(old, chunk_prose(old))])
    new = Document(path="a.md", text="fresh content")
    index.replace_documents("src_1", [(new, chunk_prose(new))])
    assert store.search([kb.id], "obsolete", 5) == []
    assert store.search([kb.id], "fresh", 5)
    assert len(list((index.dir / "text").iterdir())) == 1


def test_kb_ids_are_validated(store) -> None:
    with pytest.raises(KnowledgeNotFoundError):
        store.get("../../etc")
    with pytest.raises(ValueError):
        store.create("  ")


def test_zip_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(ValueError, match="unsafe"):
        extract_zip(archive, tmp_path / "out", max_files=10, max_total_bytes=1000)
    assert not (tmp_path / "escape.txt").exists()


def test_zip_single_folder_is_unwrapped(tmp_path: Path) -> None:
    archive = tmp_path / "repo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("repo/src/main.py", "print('hi')\n")
        zf.writestr("repo/node_modules/x/index.js", "skip")
        zf.writestr("repo/logo.png", b"\x89PNG\x00\x00")
    root = extract_zip(archive, tmp_path / "out", max_files=10, max_total_bytes=10_000)
    assert root.name == "repo"
    docs = extract_tree(root, max_files=10, max_file_bytes=10_000)
    assert [(d.path, is_code) for d, is_code in docs] == [("src/main.py", True)]


def test_normalize_citations_keeps_streamed_text_a_prefix():
    from mira_engine.utils.helpers import normalize_citations

    full = "Acite[KB/x.pdf#p.1] B navlistItem"
    assert normalize_citations(full) == "A[KB/x.pdf#p.1] B Item"
    previous = ""
    for end in range(1, len(full) + 1):
        current = normalize_citations(full[:end])
        assert current.startswith(previous)
        previous = current
