import pytest
from pathlib import Path
from medpilot.agent.tools.filesystem import _resolve_path
from medpilot.agent.tools.shell import ExecTool


def test_resolve_path_inside_allowed_dir(tmp_path):
    """Test that a path inside the allowed directory resolves correctly."""
    allowed_dir = tmp_path / "workspace"
    allowed_dir.mkdir()
    
    # Test absolute path inside allowed_dir
    inside_path_abs = allowed_dir / "test.txt"
    resolved_abs = _resolve_path(str(inside_path_abs), workspace=allowed_dir, allowed_dirs=[allowed_dir])
    assert resolved_abs == inside_path_abs.resolve()

    # Test relative path inside workspace
    inside_path_rel = "test2.txt"
    resolved_rel = _resolve_path(inside_path_rel, workspace=allowed_dir, allowed_dirs=[allowed_dir])
    assert resolved_rel == (allowed_dir / "test2.txt").resolve()


def test_resolve_path_outside_allowed_dir(tmp_path):
    """Test that a path outside the allowed directory raises a PermissionError."""
    allowed_dir = tmp_path / "workspace"
    allowed_dir.mkdir()
    
    outside_dir = tmp_path / "outside_workspace"
    outside_dir.mkdir()
    outside_path = outside_dir / "secret.txt"
    
    # Absolute path outside
    with pytest.raises(PermissionError, match="is outside allowed directories"):
        _resolve_path(str(outside_path), workspace=allowed_dir, allowed_dirs=[allowed_dir])

    # Relative path that traverses outside
    traversal_path = "../outside_workspace/secret.txt"
    with pytest.raises(PermissionError, match="is outside allowed directories"):
        _resolve_path(traversal_path, workspace=allowed_dir, allowed_dirs=[allowed_dir])


def test_exec_tool_guard_command_safe():
    """Test that safe commands are allowed when restricted to workspace."""
    tool = ExecTool(restrict_to_workspace=True)
    cwd = "/homes/dxli/Code/MedPilot"
    
    assert tool._guard_command("ls -la", cwd) is None
    assert tool._guard_command("cat src/main.py", cwd) is None
    assert tool._guard_command("pytest tests/", cwd) is None


def test_exec_tool_guard_command_traversal():
    """Test that path traversal commands are blocked."""
    tool = ExecTool(restrict_to_workspace=True)
    cwd = "/homes/dxli/Code/MedPilot"
    
    blocked_msg = "Error: Command blocked by safety guard (path traversal detected)"
    
    assert tool._guard_command("cd ..", cwd) == blocked_msg
    assert tool._guard_command("cat ../../etc/passwd", cwd) == blocked_msg
    assert tool._guard_command("ls ..\\Windows", cwd) == blocked_msg


def test_exec_tool_guard_command_absolute_outside_cwd():
    """Test that absolute paths pointing outside cwd are blocked."""
    tool = ExecTool(restrict_to_workspace=True)
    cwd = "/homes/dxli/Code/MedPilot"
    
    blocked_msg = "Error: Command blocked by safety guard (path outside working dir)"
    
    assert tool._guard_command("cat /etc/passwd", cwd) == blocked_msg
    assert tool._guard_command("ls /var/log", cwd) == blocked_msg
    assert tool._guard_command("cat /homes/dxli/Documents/file.txt", cwd) == blocked_msg

    # Note: Using absolute path within cwd should be allowed
    inside_msg = tool._guard_command("cat /homes/dxli/Code/MedPilot/README.md", cwd)
    assert inside_msg is None

