import pytest
import os
from pathlib import Path
from medpilot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from medpilot.agent.skills import BUILTIN_SKILLS_DIR

def test_read_write_separation(tmp_path):
    # Setup test workspace and dummy skill file
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    # We will simulate a file inside the builtin skills directory
    # For safety of tests, let's just use the actual BUILTIN_SKILLS_DIR and read a known file,
    # or mock it. The filesystem tools resolve to absolute paths, so we can just create a dummy "skills" dir
    # and temporarily mock BUILTIN_SKILLS_DIR?
    # No, let's test if we can read an actual existing file in BUILTIN_SKILLS_DIR (like test-driven-development/SKILL.md)
    
    # Setup tools with workspace sandbox (allowed_dir = workspace)
    read_tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
    write_tool = WriteFileTool(workspace=workspace, allowed_dir=workspace)
    
    # 1. Test Writing inside workspace (Should PASS)
    test_file_ws = workspace / "test_write.txt"
    result = write_tool.execute(path="test_write.txt", content="hello")
    assert "Successfully wrote to" in result
    assert test_file_ws.exists()
    
    # 2. Test Reading from workspace (Should PASS)
    result = read_tool.execute(path="test_write.txt")
    assert "hello" in result
    
    # 3. Test Reading from Builtin Skills (Outside workspace, but allowed by ReadFileTool)
    # We pick an arbitrary existing skill folder
    sample_skill_file = BUILTIN_SKILLS_DIR / "agent-browser" / "SKILL.md"
    if sample_skill_file.exists():
        result = read_tool.execute(path=str(sample_skill_file))
        assert not result.startswith("PermissionError"), "Read tool blocked access to builtin skills"
        assert len(result) > 0
    
    # 4. Test Writing to Builtin Skills (Should FAIL)
    # We attempt to write to a dummy file inside BUILTIN_SKILLS_DIR
    dummy_write_path = BUILTIN_SKILLS_DIR / "malicious_write.txt"
    try:
        write_tool.execute(path=str(dummy_write_path), content="hacked")
        pytest.fail("WriteFileTool allowed writing outside the workspace to builtin skills directory!")
    except PermissionError as e:
        assert str(workspace) in str(e)
        assert "outside allowed directories" in str(e) or "outside allowed directory" in str(e)
        
    # 5. Test Reading/Writing completely unauthorized directories (e.g. system root)
    try:
        read_tool.execute(path="/tmp")
        pytest.fail("ReadFileTool allowed reading /tmp")
    except PermissionError:
        pass
        
    try:
        write_tool.execute(path="/tmp/hacked.txt", content="hacked")
        pytest.fail("WriteFileTool allowed writing to /tmp")
    except PermissionError:
        pass

