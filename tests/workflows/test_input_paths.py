import pytest

from mira_engine.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from mira_engine.agent.tools.search import GlobTool, GrepTool
from mira_engine.workflows import JsonWorkflowRepository, WorkflowError, WorkflowService
from mira_engine.workflows.input_paths import normalize_input_paths, run_read_paths


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "home"))


def test_normalize_and_persist_run_scoped_grants(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    source = data / "sample.txt"
    source.write_text("evidence")
    assert normalize_input_paths([str(data), str(data / "."), str(source)]) == [str(data), str(source)]
    service = WorkflowService(JsonWorkflowRepository(tmp_path / "project", "p"))
    run_id = service.create_run(command_id="one", module_type="experiment",
        module_data={"external_read_paths": [str(source)]}).run_id
    loaded = service.repository.load_run(run_id)
    assert run_read_paths(loaded) == (source,)
    next_id = service.create_run(command_id="two", module_type="experiment",
        module_data={"parent_run_id": run_id}).run_id
    assert run_read_paths(service.repository.load_run(next_id)) == ()
    with pytest.raises(WorkflowError, match="only to experiment"):
        service.create_run(command_id="bad", module_type="delivery", module_data={"external_read_paths": [str(data)]})
    # Command retries remain idempotent even after an input disappears.
    source.unlink()
    assert service.create_run(command_id="one", module_type="experiment",
        module_data={"external_read_paths": [str(source)]}).disposition == "duplicate"


@pytest.mark.parametrize("value", ["/data", [12], ["relative/path"], [""], ["\x00"], ["https://example.org/paper"]])
def test_reject_invalid_paths(value):
    with pytest.raises((ValueError, OSError)):
        normalize_input_paths(value)


async def test_grants_allow_reads_not_writes_or_siblings_and_reset(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    source = data / "input.txt"
    source.write_text("EXTERNAL EVIDENCE")
    sibling = data / "other.txt"
    sibling.write_text("PRIVATE")
    read = ReadFileTool(workspace=project, allowed_dir=project)
    assert "outside allowed" in await read.execute(path=str(source))
    with read.allow_read_paths((source,)):
        assert "EXTERNAL EVIDENCE" in await read.execute(path=str(source))
        assert "outside allowed" in await read.execute(path=str(sibling))
    assert "outside allowed" in await read.execute(path=str(source))
    for tool, args in [
        (WriteFileTool, {"path": str(source), "content": "changed"}),
        (EditFileTool, {"path": str(source), "old_text": "EVIDENCE", "new_text": "changed"}),
    ]:
        instance = tool(workspace=project, allowed_dir=project, read_paths=(data,))
        assert "outside allowed" in await instance.execute(**args)
    assert source.read_text() == "EXTERNAL EVIDENCE"
    listing = ListDirTool(workspace=project, allowed_dir=project, read_paths=(data,))
    assert "input.txt" in await listing.execute(path=str(data))


async def test_directory_grants_do_not_follow_escaping_symlinks(tmp_path):
    project, data = tmp_path / "project", tmp_path / "data"
    project.mkdir()
    data.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE SECRET")
    (data / "good.txt").write_text("PUBLIC EVIDENCE")
    try:
        (data / "escape.txt").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")
    kwargs = dict(workspace=project, allowed_dir=project, read_paths=(data,))
    assert "outside allowed" in await ReadFileTool(**kwargs).execute(path=str(data / "escape.txt"))
    assert "No matches found" in await GrepTool(**kwargs).execute(pattern="SECRET", path=str(data))
    assert "escape.txt" not in await GlobTool(**kwargs).execute(pattern="*.txt", path=str(data))
    assert "PUBLIC EVIDENCE" in await GrepTool(**kwargs).execute(pattern="PUBLIC", path=str(data), output_mode="content")


async def test_grants_do_not_restrict_unrestricted_tools(tmp_path):
    source = tmp_path / "file.txt"
    source.write_text("unrestricted")
    tool = ReadFileTool(workspace=tmp_path, read_paths=(tmp_path / "unrelated",))
    assert "unrestricted" in await tool.execute(path=str(source))
