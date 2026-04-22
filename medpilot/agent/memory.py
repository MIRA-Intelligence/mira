"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from medpilot.agent.runner import AgentRunSpec, AgentRunner
from medpilot.agent.tools.registry import ToolRegistry
from medpilot.utils.gitstore import GitStore
from medpilot.utils.helpers import ensure_dir
from medpilot.utils.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    strip_think,
)
from medpilot.utils.prompt_templates import render_template

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
_SAVE_MEMORY_TOOL_CHOICE = {"type": "function", "function": {"name": "save_memory"}}

_MAX_SAVE_MEMORY_ATTEMPTS = 3


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    @staticmethod
    def _safe_backup_folder_component(name: str) -> str:
        """Normalize workspace labels for cross-platform backup directory names."""
        # Windows forbids <>:"/\|?* and control chars, and disallows trailing dots/spaces.
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name)
        cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
        return cleaned or "workspace"

    def __init__(self, workspace: Path, max_history_entries: int = 1000):
        from medpilot.config.paths import get_workspace_path
        import hashlib

        self.workspace = workspace
        self.project_workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / ".medpilot" / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self._legacy_project_memory_dir = ensure_dir(workspace / "memory")
        self._legacy_project_memory_file = self._legacy_project_memory_dir / "MEMORY.md"
        self.history_file = self._legacy_project_memory_dir / "HISTORY.md"
        self.history_jsonl_file = self._legacy_project_memory_dir / "history.jsonl"
        self.legacy_history_file = self.history_file
        self._legacy_history_backup_file = self._legacy_project_memory_dir / "HISTORY.md.bak"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"

        self.global_workspace = get_workspace_path(None)
        self.global_memory_dir = ensure_dir(self.global_workspace / "memory")
        self.global_memory_file = self.global_memory_dir / "MEMORY.md"
        # Avoid leaking global memory into unrelated temporary workspaces.
        self._allow_global_memory = workspace.resolve().is_relative_to(self.global_workspace.resolve())
        self._explicit_global_write = False

        if workspace.resolve() != self.global_workspace.resolve():
            workspace_hash = hashlib.md5(str(workspace.resolve()).encode()).hexdigest()[:8]
            workspace_label = self._safe_backup_folder_component(str(getattr(workspace, "name", "workspace")))
            backup_folder_name = f"{workspace_label}_{workspace_hash}"
            self.backup_dir = ensure_dir(self.global_workspace / "project_backups" / backup_folder_name)
            self.memory_backup_file = self.backup_dir / "MEMORY.md"
            self.history_backup_file = self.backup_dir / "history.jsonl"
        else:
            self.backup_dir = None

        self._git = GitStore(
            self.workspace,
            tracked_files=["SOUL.md", "USER.md", "memory/MEMORY.md"],
        )

        self._migrate_legacy_history_if_needed()

    @property
    def git(self) -> GitStore:
        return self._git

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        if self._legacy_project_memory_file.exists():
            return self._legacy_project_memory_file.read_text(encoding="utf-8")
        # Fallback to backup if local was accidentally deleted
        if self.backup_dir and self.memory_backup_file.exists():
            return self.memory_backup_file.read_text(encoding="utf-8")
        return ""

    def read_global_term(self) -> str:
        if (
            (self._allow_global_memory or self._explicit_global_write)
            and self.memory_file != self.global_memory_file
            and self.global_memory_file.exists()
        ):
            return self.global_memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")
        self._legacy_project_memory_file.parent.mkdir(parents=True, exist_ok=True)
        self._legacy_project_memory_file.write_text(content, encoding="utf-8")
        if self.backup_dir:
            self.memory_backup_file.write_text(content, encoding="utf-8")

    @staticmethod
    def read_file(path: Path) -> str:
        if path.name == "HISTORY.md":
            jsonl = path.with_name("history.jsonl")
            if jsonl.exists():
                try:
                    lines = [line for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
                    if lines:
                        return lines[-1]
                except OSError:
                    pass
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def read_memory(self) -> str:
        return self.read_long_term()

    def write_memory(self, content: str) -> None:
        self.write_long_term(content)

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> int:
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        cleaned = strip_think(entry.rstrip()) or entry.rstrip()
        with open(self.history_jsonl_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"cursor": cursor, "timestamp": ts, "content": cleaned}, ensure_ascii=False) + "\n")
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(f"{cleaned}\n\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        if self.backup_dir:
            with open(self.history_backup_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"cursor": cursor, "timestamp": ts, "content": cleaned}, ensure_ascii=False) + "\n")
        return cursor

    def _next_cursor(self) -> int:
        if self._cursor_file.exists():
            try:
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
            except (ValueError, OSError):
                pass
        last = self._read_last_jsonl_entry()
        return (last.get("cursor", 0) + 1) if last else 1

    def _read_last_jsonl_entry(self) -> dict[str, Any] | None:
        try:
            with open(self.history_jsonl_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [l for l in data.split("\n") if l.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        self._migrate_legacy_history_if_needed()
        entries: list[dict[str, Any]] = []
        source = self.history_jsonl_file
        if not source.exists() and self.history_file.exists():
            source = self.history_file
        try:
            with open(source, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("cursor", 0) > since_cursor:
                        entries.append(row)
        except FileNotFoundError:
            pass
        return entries

    def compact_history(self) -> None:
        entries = self.read_unprocessed_history(since_cursor=0)
        if len(entries) <= self.max_history_entries:
            return
        keep = entries[-self.max_history_entries :]
        with open(self.history_jsonl_file, "w", encoding="utf-8") as f:
            for entry in keep:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def _parse_timestamped_line(line: str) -> tuple[str, str] | None:
        m = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s?(.*)$", line)
        if not m:
            return None
        return m.group(1), m.group(2)

    @staticmethod
    def _is_generic_header(line: str) -> bool:
        return line.startswith("[") and "]" in line

    @staticmethod
    def _is_raw_continuation(content: str) -> bool:
        return bool(re.match(r"^(USER|ASSISTANT|SYSTEM|TOOL)(\b|:)", content))

    def _migrate_legacy_history_if_needed(self) -> None:
        if not self.legacy_history_file.exists():
            return

        if self.history_jsonl_file.exists():
            try:
                size = int(self.history_jsonl_file.stat().st_size)
            except (TypeError, ValueError):
                size = 0
            if size > 0:
                return

        raw = self.legacy_history_file.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        # Preserve legacy line endings exactly so migration backups are byte-stable across OSes.
        self._legacy_history_backup_file.write_text(text, encoding="utf-8", newline="")
        fallback_ts = datetime.fromtimestamp(self._legacy_history_backup_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

        entries: list[dict[str, Any]] = []
        current_ts: str | None = None
        current_lines: list[str] = []
        current_is_raw = False

        def flush_current() -> None:
            nonlocal current_ts, current_lines, current_is_raw
            content = "\n".join(current_lines).strip()
            if content:
                entries.append(
                    {
                        "cursor": len(entries) + 1,
                        "timestamp": current_ts or fallback_ts,
                        "content": content,
                    }
                )
            current_ts = None
            current_lines = []
            current_is_raw = False

        for line in text.splitlines():
            if current_is_raw and not line.strip() and current_lines:
                flush_current()
                continue

            parsed = self._parse_timestamped_line(line)
            if parsed is not None:
                ts, content = parsed
                if current_lines and current_is_raw and self._is_raw_continuation(content):
                    current_lines.append(content)
                    continue
                if current_lines:
                    flush_current()
                current_ts = ts
                current_lines = [content]
                current_is_raw = content.startswith("[RAW]")
                continue

            if self._is_generic_header(line):
                if current_lines:
                    flush_current()
                current_ts = fallback_ts
                current_lines = [line]
                current_is_raw = False
                continue

            if not current_lines and not line.strip():
                continue
            if not current_lines:
                current_ts = fallback_ts
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            flush_current()

        if entries:
            with open(self.history_jsonl_file, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            last_cursor = entries[-1]["cursor"]
            self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
            self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

        self.legacy_history_file.unlink(missing_ok=True)

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            try:
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = (
                f" [tools: {', '.join(message['tools_used'])}]"
                if message.get("tools_used")
                else ""
            )
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] "
                f"{message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict]) -> None:
        self.append_history(f"[RAW] {len(messages)} messages\n{self._format_messages(messages)}")

    def write_global_term(self, content: str) -> None:
        self._explicit_global_write = True
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

        # Backward compatibility for existing prompts/tests.
        if local_term and not global_term:
            return f"## Long-term Memory\n{local_term}"

        parts = []
        if global_term:
            parts.append(f"## Global System Memory (Rules & Guidelines)\n{global_term}")
        if local_term:
            parts.append(f"## Local Project Memory (Current Case/Context)\n{local_term}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _align_boundary_to_user(messages: list[dict], boundary: int) -> int:
        """Move boundary backward to sit on a user message so the
        unconsolidated window starts at a clean conversation turn."""
        while boundary > 0 and messages[boundary].get("role") != "user":
            boundary -= 1
        return boundary

    @staticmethod
    def _extract_json_dict_from_text(text: str) -> dict[str, Any] | None:
        """Extract a JSON object from raw LLM text (supports fenced blocks)."""
        stripped = text.strip()
        candidates: list[str] = []
        if stripped:
            candidates.append(stripped)

        fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", stripped, flags=re.IGNORECASE)
        for block in fenced:
            block = block.strip()
            if block:
                candidates.append(block)

        first_brace = stripped.find("{")
        last_brace = stripped.rfind("}")
        if 0 <= first_brace < last_brace:
            candidates.append(stripped[first_brace:last_brace + 1])

        for cand in candidates:
            try:
                parsed = json.loads(cand)
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, list):
                if parsed and isinstance(parsed[0], dict):
                    parsed = parsed[0]
                else:
                    continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _normalize_save_memory_args(payload: Any) -> dict[str, Any] | None:
        """Normalize provider-specific tool argument formats into a dict payload."""
        normalized: Any = payload
        for _ in range(4):
            if isinstance(normalized, str):
                try:
                    normalized = json.loads(normalized)
                except json.JSONDecodeError:
                    return None
                continue

            if isinstance(normalized, list):
                if normalized and isinstance(normalized[0], dict):
                    normalized = normalized[0]
                    continue
                return None

            if isinstance(normalized, dict):
                fn = normalized.get("function")
                if isinstance(fn, dict) and fn.get("name") == "save_memory" and "arguments" in fn:
                    normalized = fn["arguments"]
                    continue
                if normalized.get("name") == "save_memory" and "arguments" in normalized:
                    normalized = normalized["arguments"]
                    continue
                if normalized.get("tool") == "save_memory" and "arguments" in normalized:
                    normalized = normalized["arguments"]
                    continue
                break

            return None

        if not isinstance(normalized, dict):
            return None

        args = dict(normalized)
        # Backward compatibility for older test fixtures/providers.
        if "memory_update" in args and "project_memory_update" not in args:
            args["project_memory_update"] = args["memory_update"]

        if not any(
            k in args for k in ("history_entry", "project_memory_update", "workspace_memory_update")
        ):
            return None
        return args

    def _extract_save_memory_args(self, response: Any) -> dict[str, Any] | None:
        """Get normalized save_memory payload from tool_calls or JSON-text fallback."""
        for tc in response.tool_calls or []:
            if getattr(tc, "name", None) != "save_memory":
                continue
            args = self._normalize_save_memory_args(getattr(tc, "arguments", None))
            if args is not None:
                return args

        if isinstance(response.content, str) and response.content.strip():
            parsed = self._extract_json_dict_from_text(response.content)
            if parsed is not None:
                args = self._normalize_save_memory_args(parsed)
                if args is not None:
                    logger.info("Memory consolidation: recovered save_memory payload from JSON text fallback")
                    return args
        return None

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
            args: dict[str, Any] | None = None
            for attempt in range(1, _MAX_SAVE_MEMORY_ATTEMPTS + 1):
                response = await provider.chat(
                    messages=[
                        {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                        {"role": "user", "content": prompt},
                    ],
                    tools=_SAVE_MEMORY_TOOL,
                    tool_choice=_SAVE_MEMORY_TOOL_CHOICE,
                    model=model,
                )
                args = self._extract_save_memory_args(response)
                if args is not None:
                    break
                if attempt < _MAX_SAVE_MEMORY_ATTEMPTS:
                    logger.warning(
                        "Memory consolidation: save_memory payload missing (attempt {}/{}), retrying",
                        attempt,
                        _MAX_SAVE_MEMORY_ATTEMPTS,
                    )
            if args is None:
                logger.warning(
                    "Memory consolidation: LLM did not provide save_memory payload after {} attempts, skipping",
                    _MAX_SAVE_MEMORY_ATTEMPTS,
                )
                return False

            wrote_history = False
            wrote_project = False
            wrote_workspace = False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)
                wrote_history = True
            
            if proj_update := args.get("project_memory_update"):
                if not isinstance(proj_update, str):
                    proj_update = json.dumps(proj_update, ensure_ascii=False)
                if proj_update != current_local:
                    self.write_long_term(proj_update)
                    wrote_project = True
                    
            if work_update := args.get("workspace_memory_update"):
                if not isinstance(work_update, str):
                    work_update = json.dumps(work_update, ensure_ascii=False)
                if work_update != current_global:
                    self.write_global_term(work_update)
                    wrote_workspace = True

            if archive_all:
                session.last_consolidated = 0
            else:
                boundary = len(session.messages) - keep_count
                # Align boundary to a user message so the unconsolidated
                # window never starts mid-tool-call-sequence.
                boundary = self._align_boundary_to_user(session.messages, boundary)
                session.last_consolidated = boundary
            logger.info(
                "Memory consolidation writes: history={}, project={}, workspace={}",
                wrote_history,
                wrote_project,
                wrote_workspace,
            )
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False


class Consolidator:
    """Lightweight consolidation: summarize old messages into history."""

    _MAX_CONSOLIDATION_ROUNDS = 5
    _SAFETY_BUFFER = 1024

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(self, session, tokens_to_remove: int) -> tuple[int, int] | None:
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None
        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)
        if last_boundary is not None:
            return last_boundary
        if len(session.messages) > start + 1:
            # Fallback for strict short windows: allow archiving all but newest message.
            return len(session.messages) - 1, removed_tokens
        return None

    def estimate_session_prompt_tokens(self, session) -> tuple[int, str]:
        history = session.get_history(max_messages=0)
        channel, chat_id = (
            session.key.split(":", 1) if ":" in session.key else (None, None)
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive(self, messages: list[dict]) -> bool:
        if not messages:
            return False
        try:
            formatted = MemoryStore._format_messages(messages)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template("agent/consolidator_archive.md", strip=True),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            self.store.append_history(response.content or "[no summary]")
            return True
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return True

    async def maybe_consolidate_by_tokens(self, session) -> None:
        if not session.messages or self.context_window_tokens <= 0:
            return
        lock = self.get_lock(session.key)
        async with lock:
            budget = (
                self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
            )
            target = budget // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0 or estimated < budget:
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return
                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return
                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated : end_idx]
                if not chunk:
                    return
                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                if not await self.archive(chunk):
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)
                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return


class Dream:
    """Two-phase memory processor for unprocessed history entries."""

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def _build_tools(self) -> ToolRegistry:
        from medpilot.agent.tools.filesystem import EditFileTool, ReadFileTool

        tools = ToolRegistry()
        workspace = self.store.project_workspace
        tools.register(ReadFileTool(workspace=workspace, allowed_dir=workspace))
        tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace))
        return tools

    async def run(self) -> bool:
        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return False

        batch = entries[: self.max_batch_size]
        history_text = "\n".join(f"[{e['timestamp']}] {e['content']}" for e in batch)
        current_date = datetime.now().strftime("%Y-%m-%d")
        current_memory = self.store.read_memory() or "(empty)"
        current_soul = self.store.read_soul() or "(empty)"
        current_user = self.store.read_user() or "(empty)"
        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
        )

        try:
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template("agent/dream_phase1.md", strip=True),
                    },
                    {"role": "user", "content": f"## Conversation History\n{history_text}\n\n{file_context}"},
                ],
                tools=None,
                tool_choice=None,
            )
            analysis = phase1_response.content or ""
        except Exception:
            logger.exception("Dream Phase 1 failed")
            return False

        phase2_prompt = f"## Analysis Result\n{analysis}\n\n{file_context}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": render_template("agent/dream_phase2.md", strip=True)},
            {"role": "user", "content": phase2_prompt},
        ]
        try:
            result = await self._runner.run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=self._tools,
                    model=self.model,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    fail_on_tool_error=False,
                )
            )
        except Exception:
            logger.exception("Dream Phase 2 failed")
            result = None

        new_cursor = batch[-1]["cursor"]
        self.store.set_last_dream_cursor(new_cursor)
        self.store.compact_history()
        return bool(result and result.stop_reason == "completed")
