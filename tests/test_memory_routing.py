"""Project memory lives in <project>/memory; long-term memory in MIRA_HOME/mind."""

import os
from pathlib import Path

from mira_engine.agent.memory import MemoryStore


def _mind() -> Path:
    return Path(os.environ["MIRA_HOME"]) / "mind"


def test_memory_path_initialization(tmp_path):
    project_ws = tmp_path / "my_project"
    store = MemoryStore(workspace=project_ws)

    assert store.is_project
    assert store.mind_dir == _mind()
    assert store.global_memory_file == _mind() / "MEMORY.md"
    assert store.soul_file == _mind() / "SOUL.md"
    assert store.memory_dir == project_ws / "memory"
    assert store.memory_file == project_ws / "memory" / "MEMORY.md"
    assert store.history_jsonl_file == project_ws / "memory" / "history.jsonl"
    assert not (project_ws / ".mira").exists()


def test_memory_context_combination(tmp_path):
    store = MemoryStore(workspace=tmp_path / "my_project")
    store.write_long_term("LOCAL KNOWLEDGE")
    # Only Dream writes the mind; simulate one of its edits.
    store.global_memory_file.write_text("GLOBAL KNOWLEDGE", encoding="utf-8")

    context = store.get_memory_context()
    assert context == "## Long-term Memory\nGLOBAL KNOWLEDGE\n\n## Project Memory\nLOCAL KNOWLEDGE"


def test_unedited_memory_template_is_not_injected(tmp_path):
    from importlib.resources import files as pkg_files

    store = MemoryStore(workspace=tmp_path / "my_project")
    template = (pkg_files("mira_engine") / "templates" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    store.global_memory_file.write_text(template, encoding="utf-8")
    assert store.get_memory_context() == ""


def test_global_memory_is_shared_across_projects(tmp_path):
    store_a = MemoryStore(workspace=tmp_path / "project_a")
    store_a.write_long_term("PROJECT A ONLY")
    store_a.global_memory_file.write_text("SHARED GLOBAL FACT", encoding="utf-8")

    store_b = MemoryStore(workspace=tmp_path / "project_b")
    store_b.write_long_term("PROJECT B ONLY")

    assert store_b.read_global_term() == "SHARED GLOBAL FACT"
    context_b = store_b.get_memory_context()
    assert "SHARED GLOBAL FACT" in context_b
    assert "PROJECT B ONLY" in context_b
    assert "PROJECT A ONLY" not in context_b


def test_hidden_project_memory_moves_to_visible_folder(tmp_path):
    project = tmp_path / "PRJ-0001"
    hidden = project / ".mira" / "memory"
    hidden.mkdir(parents=True)
    (hidden / "MEMORY.md").write_text("what the agent saw", encoding="utf-8")
    (hidden / ".dream_cursor").write_text("7", encoding="utf-8")
    (hidden / ".cursor").write_text("9", encoding="utf-8")
    (project / ".mira" / "project.json").write_text("{}", encoding="utf-8")
    (project / "memory").mkdir()
    (project / "memory" / "MEMORY.md").write_text("stale dual-write copy", encoding="utf-8")

    store = MemoryStore(workspace=project)

    assert store.read_long_term() == "what the agent saw"
    assert store.get_last_dream_cursor() == 7
    assert not hidden.exists()
    assert (project / ".mira" / "project.json").exists()
