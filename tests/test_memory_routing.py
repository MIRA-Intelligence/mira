import pytest
from pathlib import Path
from medpilot.agent.memory import MemoryStore

def test_memory_path_initialization(tmp_path, monkeypatch):
    # Mock get_workspace_path to return a fixed global path
    global_ws = tmp_path / "global_workspace"
    project_ws = tmp_path / "my_project"
    
    import medpilot.config.paths
    monkeypatch.setattr(medpilot.config.paths, "get_workspace_path", lambda x: global_ws)
    
    store = MemoryStore(workspace=project_ws)
    
    assert store.global_workspace == global_ws
    assert store.global_memory_dir == global_ws / "memory"
    assert store.global_memory_file == global_ws / "memory" / "MEMORY.md"
    
    assert store.project_workspace == project_ws
    assert store.memory_dir == project_ws / ".medpilot" / "memory"
    assert store.memory_file == project_ws / ".medpilot" / "memory" / "MEMORY.md"
    
    # Check that backup is enabled since project != global
    assert store.backup_dir is not None

def test_memory_context_combination(tmp_path, monkeypatch):
    global_ws = tmp_path / "global_workspace"
    project_ws = tmp_path / "my_project"
    
    import medpilot.config.paths
    monkeypatch.setattr(medpilot.config.paths, "get_workspace_path", lambda x: global_ws)
    
    store = MemoryStore(workspace=project_ws)
    
    # Write some distinct contents
    store.write_long_term("LOCAL KNOWLEDGE")
    store.write_global_term("GLOBAL KNOWLEDGE")
    
    # Check that files were created
    assert store.memory_file.exists()
    assert store.memory_file.read_text(encoding="utf-8") == "LOCAL KNOWLEDGE"
    
    assert store.global_memory_file.exists()
    assert store.global_memory_file.read_text(encoding="utf-8") == "GLOBAL KNOWLEDGE"
    
    context = store.get_memory_context()
    assert "## Global System Memory (Rules & Guidelines)" in context
    assert "GLOBAL KNOWLEDGE" in context
    assert "## Local Project Memory (Current Case/Context)" in context
    assert "LOCAL KNOWLEDGE" in context
    
def test_memory_same_workspace(tmp_path, monkeypatch):
    # If the user acts directly in the global workspace
    global_ws = tmp_path / "global_workspace"
    
    import medpilot.config.paths
    monkeypatch.setattr(medpilot.config.paths, "get_workspace_path", lambda x: global_ws)
    
    store = MemoryStore(workspace=global_ws)
    
    # Backup should be None when workspace is global
    assert store.backup_dir is None

