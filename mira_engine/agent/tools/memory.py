"""Scoped edits to the shared personal and project memory stores."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mira_engine.agent.tools.base import Tool, tool_parameters

if TYPE_CHECKING:
    from mira_engine.agent.memory import MemoryStore


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["personal", "project"]},
            "old_text": {
                "type": "string",
                "description": "Exact, unique text to replace. Use empty text only to initialize empty memory.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text; empty text deletes the matched fact.",
            },
        },
        "required": ["scope", "old_text", "new_text"],
    }
)
class EditMemoryTool(Tool):
    """Edit memory without granting filesystem access outside the project."""

    def __init__(self, store: MemoryStore):
        self.store = store

    @property
    def name(self) -> str:
        return "edit_memory"

    @property
    def description(self) -> str:
        return (
            "Edit long-term memory with an exact text replacement. "
            "Use personal for confirmed user facts, preferences and reusable workflows "
            "shared across projects; use project for facts specific to this project."
        )

    async def execute(self, scope: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        if scope not in {"personal", "project"}:
            return "Error: scope must be personal or project"
        local = self.store.read_long_term()
        shared = self.store.read_global_term()
        current = shared if scope == "personal" else local
        if not old_text:
            if current:
                return (
                    "Error: Memory is not empty; provide exact old_text to preserve existing facts."
                )
            updated = new_text
        elif current.count(old_text) != 1:
            return "Error: old_text must match exactly once in the current memory."
        else:
            updated = current.replace(old_text, new_text, 1)
        if not self.store.commit_consolidated_memories(
            expected_local=local,
            local_update=updated if scope == "project" else None,
            expected_global=shared,
            global_update=updated if scope == "personal" else None,
        ):
            return "Error: Memory changed concurrently; retry with the latest contents."
        return f"Successfully updated {scope} memory."
