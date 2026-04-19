from __future__ import annotations

from typing import Any

from medpilot.agent.tools.base import Tool
from medpilot.agent.tools.registry import ToolRegistry


class _EchoTool(Tool):
    def __init__(self, fail: bool = False):
        self.fail = fail

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        }

    async def execute(self, value: int, **kwargs: Any) -> str:
        if self.fail:
            raise RuntimeError("boom")
        return f"echo:{value}"


class _ErrorStringTool(_EchoTool):
    async def execute(self, value: int, **kwargs: Any) -> str:
        return "Error: downstream failed"


def test_registry_register_unregister_has_and_len() -> None:
    reg = ToolRegistry()
    tool = _EchoTool()
    reg.register(tool)
    assert reg.has("echo")
    assert "echo" in reg
    assert len(reg) == 1
    assert reg.get("echo") is tool
    reg.unregister("echo")
    assert not reg.has("echo")


async def test_registry_execute_success_with_casting() -> None:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    result = await reg.execute("echo", {"value": "7"})
    assert result == "echo:7"


async def test_registry_execute_reports_validation_error() -> None:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    result = await reg.execute("echo", {"value": "abc"})
    assert "Invalid parameters for tool 'echo'" in result
    assert "try a different approach" in result


async def test_registry_execute_reports_missing_tool() -> None:
    reg = ToolRegistry()
    result = await reg.execute("missing", {})
    assert "Tool 'missing' not found" in result


async def test_registry_execute_wraps_tool_exceptions() -> None:
    reg = ToolRegistry()
    reg.register(_EchoTool(fail=True))
    result = await reg.execute("echo", {"value": 1})
    assert "Error executing echo: boom" in result


async def test_registry_execute_appends_hint_to_error_string() -> None:
    reg = ToolRegistry()
    reg.register(_ErrorStringTool())
    result = await reg.execute("echo", {"value": 2})
    assert result.startswith("Error: downstream failed")
    assert "try a different approach" in result


def test_registry_definitions_include_registered_tools() -> None:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    defs = reg.get_definitions()
    assert defs[0]["function"]["name"] == "echo"
