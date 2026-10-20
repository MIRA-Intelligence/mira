"""Tool registry for dynamic tool management."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from mira_engine.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._visible_tools: ContextVar[frozenset[str] | None] = ContextVar(
            f"visible_tools_{id(self)}", default=None
        )
        self._execution_guard: ContextVar[
            Callable[[str, dict[str, Any]], Awaitable[str | None]] | None
        ] = ContextVar(f"execution_guard_{id(self)}", default=None)

    def _is_visible(self, name: str) -> bool:
        visible = self._visible_tools.get()
        return visible is None or name in visible

    @contextmanager
    def restrict_to(
        self,
        names: set[str] | frozenset[str],
        *,
        execution_guard: Callable[[str, dict[str, Any]], Awaitable[str | None]] | None = None,
    ):
        """Limit definitions and execution to this task-local capability set."""
        visible_token = self._visible_tools.set(frozenset(names))
        guard_token = self._execution_guard.set(execution_guard)
        try:
            yield
        finally:
            self._execution_guard.reset(guard_token)
            self._visible_tools.reset(visible_token)

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        if not self._is_visible(name):
            return None
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools and self._is_visible(name)

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        names = sorted(
            (name for name in self._tools if self._is_visible(name)),
            key=lambda name: (name.startswith("mcp_"), name),
        )
        return [self._tools[name].to_schema() for name in names]

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        hint = "\n\n[Analyze the error above and try a different approach.]"

        tool = self.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            guard = self._execution_guard.get()
            if guard is not None:
                blocked = await guard(name, params)
                if blocked is not None:
                    return blocked
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + hint
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + hint
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + hint

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return [name for name in self._tools if self._is_visible(name)]

    @property
    def all_tool_names(self) -> list[str]:
        """Get registered names without applying a task-local visibility scope."""
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)
