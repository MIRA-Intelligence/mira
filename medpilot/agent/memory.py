"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from medpilot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from medpilot.providers.base import LLMProvider
    from medpilot.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to appropriate storage. Actively classify knowledge into global rules vs project specifics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A log of key events/decisions. Start with [YYYY-MM-DD HH:MM].",
                    },
                    "project_memory_update": {
                        "type": "string",
                        "description": "Specific background for the CURRENT PROJECT ONLY (architecture, local bugs, specific API key paths, file names). Return unchanged if nothing new.",
                    },
                    "workspace_memory_update": {
                        "type": "string",
                        "description": "Global, reusable knowledge (Python tips, general DL concepts, cross-project configs). Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "project_memory_update", "workspace_memory_update"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        from medpilot.config.paths import get_workspace_path
        import hashlib
        from medpilot.utils.helpers import get_medpilot_dir


        self.project_workspace = workspace
        self.memory_dir = ensure_dir(get_medpilot_dir(workspace) / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

        self.global_workspace = get_workspace_path(None)
        self.global_memory_dir = ensure_dir(self.global_workspace / "memory")
        self.global_memory_file = self.global_memory_dir / "MEMORY.md"

        if workspace.resolve() != self.global_workspace.resolve():
            workspace_hash = hashlib.md5(str(workspace.resolve()).encode()).hexdigest()[:8]
            backup_folder_name = f"{workspace.name}_{workspace_hash}"
            self.backup_dir = ensure_dir(self.global_workspace / "project_backups" / backup_folder_name)
            self.memory_backup_file = self.backup_dir / "MEMORY.md"
            self.history_backup_file = self.backup_dir / "HISTORY.md"
        else:
            self.backup_dir = None

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        # Fallback to backup if local was accidentally deleted
        if self.backup_dir and self.memory_backup_file.exists():
            return self.memory_backup_file.read_text(encoding="utf-8")
        return ""

    def read_global_term(self) -> str:
        if self.memory_file != self.global_memory_file and self.global_memory_file.exists():
            return self.global_memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")
        if self.backup_dir:
            self.memory_backup_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")
        if self.backup_dir:
            with open(self.history_backup_file, "a", encoding="utf-8") as f:
                f.write(entry.rstrip() + "\n\n")

    def write_global_term(self, content: str) -> None:
        if self.memory_file.resolve() != self.global_memory_file.resolve():
            from medpilot.utils.helpers import ensure_dir
            ensure_dir(self.global_memory_file.parent)
            self.global_memory_file.write_text(content, encoding="utf-8")
        else:
            # If they are exactly the same, writing to long_term is enough
            pass

    def get_memory_context(self) -> str:
        global_term = self.read_global_term()
        local_term = self.read_long_term()
        
        parts = []
        if global_term:
            parts.append(f"## Global System Memory (Rules & Guidelines)\n{global_term}")
        if local_term:
            parts.append(f"## Local Project Memory (Current Case/Context)\n{local_term}")
            
        return "\n\n".join(parts) if parts else ""

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")

        current_local = self.read_long_term()
        current_global = self.read_global_term()
        prompt = f"""Process this conversation and partition the memory using the save_memory tool.
You MUST analyze the knowledge and separate it:
- workspace_memory_update: Global, deep learning facts, Python rules, MedPilot workflows.
- project_memory_update: Specific bugs, local paths, architecture of the current project.

## Current Workspace/Global Memory
{current_global or "(empty)"}

## Current Local Project Memory
{current_local or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        try:
            response = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # Some providers return arguments as a JSON string instead of dict
            if isinstance(args, str):
                args = json.loads(args)
            # Some providers return arguments as a list (handle edge case)
            if isinstance(args, list):
                if args and isinstance(args[0], dict):
                    args = args[0]
                else:
                    logger.warning("Memory consolidation: unexpected arguments as empty or non-dict list")
                    return False
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)
            
            if proj_update := args.get("project_memory_update"):
                if not isinstance(proj_update, str):
                    proj_update = json.dumps(proj_update, ensure_ascii=False)
                if proj_update != current_local:
                    self.write_long_term(proj_update)
                    
            if work_update := args.get("workspace_memory_update"):
                if not isinstance(work_update, str):
                    work_update = json.dumps(work_update, ensure_ascii=False)
                if work_update != current_global:
                    self.write_global_term(work_update)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False
