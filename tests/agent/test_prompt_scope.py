"""Per-turn tool visibility and bootstrap-template selection keep the fixed prompt small."""

from importlib.resources import files as pkg_files
from pathlib import Path

import pytest

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.bootstrap_history import SHIPPED_TEMPLATE_DIGESTS, template_digest
from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.tools.base import Tool
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.bus.events import InboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig
from mira_engine.providers.base import LLMProvider, LLMResponse

MODULE_RUN_TOOLS = {
    "request_workflow_interaction",
    "record_initiation_research_result",
    "publish_workflow_artifact",
    "record_workflow_experiment",
    "complete_workflow_run",
}


class _Echo(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "echo"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return self._name


async def test_hidden_tools_leave_definitions_and_execution() -> None:
    registry = ToolRegistry()
    for name in ("a", "b", "c"):
        registry.register(_Echo(name))
    registry.hide_for_turn({"b"})
    assert [d["function"]["name"] for d in registry.get_definitions()] == ["a", "c"]
    assert (await registry.execute("b", {})).startswith("Error: Tool 'b' not found")
    with registry.restrict_to({"a", "b"}):
        assert [d["function"]["name"] for d in registry.get_definitions()] == ["a"]
    registry.hide_for_turn(set())
    assert len(registry.get_definitions()) == 3


class _ToolCapturingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.tool_names: list[set[str]] = []

    def get_default_model(self) -> str:
        return "dummy/scope"

    async def chat(self, **kwargs):
        self.tool_names.append({t["function"]["name"] for t in kwargs.get("tools") or []})
        return LLMResponse(content="Done")


@pytest.mark.parametrize("metadata,hidden", [
    ({}, MODULE_RUN_TOOLS | {"set_plan"}),
    ({"_plan_event": "answer"}, MODULE_RUN_TOOLS),
])
async def test_plain_turns_do_not_carry_workflow_tools(tmp_path, monkeypatch, metadata, hidden) -> None:
    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "home"))
    provider = _ToolCapturingProvider()
    loop = BaseAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                         model="dummy/scope", channels_config=ChannelsConfig())
    registered = set(loop.tools.all_tool_names)
    assert MODULE_RUN_TOOLS | {"set_plan"} <= registered
    await loop._process_message(InboundMessage(channel="cli", sender_id="u", chat_id="c", content="hi",
                                               metadata=metadata))
    assert provider.tool_names[0] == registered - hidden


def test_module_run_turn_keeps_its_own_scope(tmp_path) -> None:
    loop = BaseAgentLoop.__new__(BaseAgentLoop)
    loop.tools = ToolRegistry()
    for name in (*MODULE_RUN_TOOLS, "set_plan", "read_file"):
        loop.tools.register(_Echo(name))
    loop._scope_turn_tools({"workflow_run_id": "run-1"})
    assert len(loop.tools.get_definitions()) == len(MODULE_RUN_TOOLS) + 2


def _builtin(name: str) -> str:
    return (pkg_files("mira_engine") / "templates" / name).read_text(encoding="utf-8")


def test_current_templates_are_registered() -> None:
    # Changing a template? Add its new digest to agent/bootstrap_history.py so
    # workspaces holding the previous version are recognised as unedited.
    for name, digests in SHIPPED_TEMPLATE_DIGESTS.items():
        assert template_digest(_builtin(name)) in digests, name


def _prompt_with_workspace_files(tmp_path: Path, monkeypatch, files: dict[str, str]) -> str:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    for name, text in files.items():
        (workspace / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr("mira_engine.config.paths.get_workspace_path", lambda _=None: workspace)
    return ContextBuilder(workspace)._load_bootstrap_files()


def test_unedited_old_template_copy_follows_current_template(tmp_path, monkeypatch) -> None:
    import subprocess

    old = subprocess.run(
        ["git", "show", "343c045:mira_engine/templates/TOOLS.md"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2],
    )
    if old.returncode != 0:
        pytest.skip("git history unavailable")
    assert template_digest(old.stdout) in SHIPPED_TEMPLATE_DIGESTS["TOOLS.md"]
    prompt = _prompt_with_workspace_files(tmp_path, monkeypatch, {"TOOLS.md": old.stdout})
    assert "exec — Background Jobs" not in prompt
    assert _builtin("TOOLS.md").strip().splitlines()[-1] in prompt


def test_edited_files_are_kept_and_unedited_user_md_is_dropped(tmp_path, monkeypatch) -> None:
    prompt = _prompt_with_workspace_files(tmp_path, monkeypatch, {
        "USER.md": _builtin("USER.md"),
        "SOUL.md": "# Soul\n\nCustom persona line.",
    })
    assert "## USER.md" not in prompt
    assert "Custom persona line." in prompt

    edited = _prompt_with_workspace_files(tmp_path / "again", monkeypatch, {
        "USER.md": _builtin("USER.md").replace("MIRA User", "Ada"),
    })
    assert "## USER.md" in edited and "Ada" in edited
