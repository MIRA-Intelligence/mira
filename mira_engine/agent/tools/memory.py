"""Scoped edits to the shared personal and project memory stores."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mira_engine.agent.tools.base import Tool, tool_parameters
from mira_engine.utils.locks import atomic_write_text, interprocess_lock

if TYPE_CHECKING:
    from mira_engine.agent.memory import MemoryStore


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["personal", "project", "user"]},
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
            "Use user for stable identity and preferences in shared USER.md; use personal for reusable knowledge "
            "shared across projects; use project for facts specific to this project."
        )

    async def execute(self, scope: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        if scope not in {"personal", "project", "user"}:
            return "Error: scope must be personal, project or user"
        local = self.store.read_long_term()
        shared = self.store.read_global_term()
        user_file = self.store.global_workspace / "USER.md"
        current = (self.store.read_global_user() if scope == "user"
                   else shared if scope == "personal" else local)
        if not old_text:
            if current:
                return (
                    "Error: Memory is not empty; provide exact old_text to preserve existing facts.\n"
                    f"Current contents:\n{current}"
                )
            updated = new_text
        elif current.count(old_text) != 1:
            return f"Error: old_text must match exactly once. Current contents:\n{current}"
        else:
            updated = current.replace(old_text, new_text, 1)
        if scope == "user":
            with interprocess_lock(user_file):
                latest = self.store.read_global_user()
                if latest != current:
                    return f"Error: USER.md changed concurrently. Current contents:\n{latest}"
                atomic_write_text(user_file, updated)
            return "Successfully updated shared USER.md."
        if not self.store.commit_consolidated_memories(
            expected_local=local,
            local_update=updated if scope == "project" else None,
            expected_global=shared,
            global_update=updated if scope == "personal" else None,
        ):
            latest = self.store.read_global_term() if scope == "personal" else self.store.read_long_term()
            return f"Error: Memory changed concurrently. Current contents:\n{latest}"
        return f"Successfully updated {scope} memory."
