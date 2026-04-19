import pytest
import os
import asyncio
from pathlib import Path
from medpilot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from medpilot.agent.skills import BUILTIN_SKILLS_DIR

@pytest.mark.asyncio
async def test_read_write_separation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    read_tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
    write_tool = WriteFileTool(workspace=workspace, allowed_dir=workspace)
    
    test_file_ws = workspace / "test_write.txt"
    result = await write_tool.execute(path="test_write.txt", content="hello")
    assert "Successfully wrote" in result
    assert test_file_ws.exists()
    
    result = await read_tool.execute(path="test_write.txt")
    assert "hello" in result
    
    sample_skill_file = BUILTIN_SKILLS_DIR / "agent-browser" / "SKILL.md"
    if sample_skill_file.exists():
        result = await read_tool.execute(path=str(sample_skill_file))
        assert not result.startswith("Error:")
        assert len(result) > 0
    
    dummy_write_path = BUILTIN_SKILLS_DIR / "malicious_write.txt"
    result = await write_tool.execute(path=str(dummy_write_path), content="hacked")
    assert "Error:" in result
    assert "outside allowed directories" in result or "outside allowed directory" in result
        
    result = await read_tool.execute(path="/tmp")
    assert "Error:" in result
        
    result = await write_tool.execute(path="/tmp/hacked.txt", content="hacked")
    assert "Error:" in result

