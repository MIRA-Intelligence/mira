"""Regression tests for the Slides (HTML) and Jupyter Notebook delivery skills."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from mira_engine.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader

DOCS = BUILTIN_SKILLS_DIR / "builtin_skills" / "documents"
BENTO = DOCS / "bento-slides"
NOTEBOOK = DOCS / "jupyter-notebook-export"

pytestmark = pytest.mark.skipif(not BENTO.exists(), reason="mira-skills submodule not checked out")


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deck() -> dict:
    return {
        "format": "bento/slides",
        "version": 1,
        "title": "T <x>",
        "size": {"width": 1280, "height": 720},
        "theme": {"background": "#000", "color": "#fff", "accent": "#f00", "fontFamily": "sans-serif"},
        "slides": [
            {
                "id": "s1",
                "notes": "n",
                "elements": [
                    {
                        "id": "t1", "type": "text", "x": 96, "y": 100, "w": 800, "h": 100,
                        "html": "</script>", "fontFamily": "sans-serif", "align": "left",
                    }
                ],
            }
        ],
    }


def test_new_skills_are_discovered(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path)
    names = {skill["name"] for skill in loader.list_skills(filter_unavailable=False)}
    assert {"bento-slides", "jupyter-notebook-export"} <= names
    for name in ("bento-slides", "jupyter-notebook-export"):
        assert len((loader.get_skill_metadata(name) or {}).get("description", "")) <= 250


def test_bento_bundle_is_offline_and_self_contained() -> None:
    app = (BENTO / "assets" / "Bento_Slides.bento.html").read_text(encoding="utf-8")
    assert 'id="bento-doc"' in app
    assert (BENTO / "references" / "agents.md").is_file()
    assert (BENTO / "LICENSE").is_file()
    assert "curl" not in (BENTO / "SKILL.md").read_text(encoding="utf-8")


def test_build_deck_injects_escaped_readonly_document(tmp_path: Path) -> None:
    build = _load(BENTO / "scripts" / "build_deck.py")
    source = tmp_path / "deck.json"
    source.write_text(json.dumps(_deck()), encoding="utf-8")
    out = tmp_path / "out.bento.html"
    assert build.main(["build_deck.py", "build", str(source), str(out)]) == 0

    html = out.read_text(encoding="utf-8")
    block = re.search(r'id="bento-doc">(.*?)</script>', html, re.DOTALL).group(1)
    assert "</script>" not in block  # every "<" is escaped
    doc = json.loads(block)
    assert doc["readonly"] is True and doc["slides"][0]["elements"][0]["html"] == "</script>"
    assert build.main(["build_deck.py", "check", str(out)]) == 0


def test_build_deck_editable_flag_and_validation(tmp_path: Path) -> None:
    build = _load(BENTO / "scripts" / "build_deck.py")
    source = tmp_path / "deck.json"
    source.write_text(json.dumps(_deck()), encoding="utf-8")
    out = tmp_path / "e.bento.html"
    assert build.main(["build_deck.py", "build", str(source), str(out), "--editable"]) == 0
    assert '"readonly"' not in out.read_text(encoding="utf-8")

    bad = _deck()
    bad["docId"] = "x"
    bad["slides"][0]["elements"][0]["src"] = "https://example.com/a.png"
    errors, _ = build.validate(bad)
    assert any("docId" in e for e in errors) and any("external image URL" in e for e in errors)


def test_notebook_build_and_check(tmp_path: Path) -> None:
    nb_script = _load(NOTEBOOK / "scripts" / "build_notebook.py")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "title": "Demo", "parameters": {"SEED": 1},
        "sections": [{"title": "exp1", "code": ["print(1)"]}],
    }), encoding="utf-8")
    out = tmp_path / "r.ipynb"
    assert nb_script.main(["build_notebook.py", "build", str(spec), str(out)]) == 0

    notebook = json.loads(out.read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4
    assert any("PROJECT_ROOT" in "".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code")
    # Freshly built notebooks have not run, so check must refuse them.
    assert nb_script.check(str(out))

    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            cell["execution_count"] = 1
    out.write_text(json.dumps(notebook), encoding="utf-8")
    assert nb_script.check(str(out)) == []

    notebook["cells"][2]["outputs"] = [{"output_type": "error", "ename": "ValueError", "evalue": "boom"}]
    out.write_text(json.dumps(notebook), encoding="utf-8")
    assert any("ValueError" in problem for problem in nb_script.check(str(out)))
