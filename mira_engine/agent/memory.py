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

from mira_engine.agent.runner import AgentRunner, AgentRunSpec
from mira_engine.agent.skill_turn import invoked_skills_metadata
from mira_engine.agent.token_usage import record_token_usage
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.utils.gitstore import GitStore
from mira_engine.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    strip_think,
)
from mira_engine.utils.locks import (
    atomic_write_text,
    interprocess_lock,
    locked_write_text,
)
from mira_engine.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from mira_engine.providers.base import LLMProvider
    from mira_engine.session.manager import Session


_HISTORY_ENTRY_PARAM = {
    "type": "string",
    "description": "A log of key events/decisions. Start with [YYYY-MM-DD HH:MM].",
}
_PROJECT_MEMORY_PARAM = {
    "type": "string",
    "description": (
        "The full updated project MEMORY.md: decisions, experiment results, data and file locations, "
        "pitfalls, current status. Return it unchanged if nothing new."
    ),
}


def _save_memory_tool(project: bool) -> list[dict[str, Any]]:
    properties: dict[str, Any] = {"history_entry": _HISTORY_ENTRY_PARAM}
    if project:
        properties["project_memory_update"] = _PROJECT_MEMORY_PARAM
    return [{
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the consolidation of the conversation.",
            "parameters": {"type": "object", "properties": properties, "required": list(properties)},
        },
    }]


_SAVE_MEMORY_TOOL_CHOICE = {"type": "function", "function": {"name": "save_memory"}}

_MAX_SAVE_MEMORY_ATTEMPTS = 3


_MIND_FILES = ("MEMORY.md", "USER.md", "SOUL.md")
_MIND_MEMORY_SEED = "# Long-term Memory\n"


def _builtin_template(name: str) -> str:
    from importlib.resources import files as pkg_files

    try:
        return (pkg_files("mira_engine") / "templates" / name).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _is_memory_scaffold(text: str) -> bool:
    """True for the shipped MEMORY.md template: headings and "(placeholder)" lines only."""
    for line in text.splitlines():
        s = line.strip()
        if (
            not s
            or s.startswith("#")
            or s == "---"
            or (s.startswith("(") and s.endswith(")"))
            or (s.startswith("*") and s.endswith("*"))
            or s.startswith("This file stores")
        ):
            continue
        return False
    return True


def _is_unedited(name: str, text: str) -> bool:
    from mira_engine.agent.bootstrap_history import is_shipped_template

    if name == "MEMORY.md":
        return _is_memory_scaffold(text)
    return not text.strip() or is_shipped_template(name, text)


def migrate_to_mind_dir(*workspaces: Path) -> list[str]:
    """Move the long-term memory a workspace used to hold into MIRA_HOME/mind.

    Earlier layouts kept SOUL.md, USER.md, memory/MEMORY.md and the history
    log in the default workspace, plus a hidden ``.mira/memory`` copy. A file
    only moves when the mind does not already hold real content, so running
    this on every start is safe. Returns the names that were moved.
    """
    from mira_engine.config.paths import get_mind_dir, get_workspace_path

    mind = get_mind_dir()
    sources = list(dict.fromkeys(
        ws.expanduser().resolve(strict=False)
        for ws in (get_workspace_path(None), *workspaces)
        if isinstance(ws, Path)
    ))
    moved: list[str] = []

    def _move(src: Path, target: Path) -> None:
        if not src.is_file() or src.resolve(strict=False) == target.resolve(strict=False):
            return
        if not _is_unedited(target.name, _read_text(target)):
            return
        text = _read_text(src)
        if not _is_unedited(target.name, text):
            atomic_write_text(target, text)
            moved.append(target.name)
        src.unlink(missing_ok=True)

    for ws in sources:
        for name in ("SOUL.md", "USER.md"):
            _move(ws / name, mind / name)
        _move(ws / "memory" / "MEMORY.md", mind / "MEMORY.md")
        # The default workspace's hidden copy held what plain chats consolidated.
        hidden = ws / ".mira" / "memory"
        leftover = _read_text(hidden / "MEMORY.md").strip()
        if not _is_memory_scaffold(leftover) and leftover not in _read_text(mind / "MEMORY.md"):
            current = _read_text(mind / "MEMORY.md").rstrip()
            atomic_write_text(mind / "MEMORY.md", f"{current}\n\n{leftover}\n" if current else f"{leftover}\n")
            moved.append(".mira/memory/MEMORY.md")
        (hidden / "MEMORY.md").unlink(missing_ok=True)
        history = ws / "memory" / "history.jsonl"
        if history.is_file() and not (mind / "history.jsonl").exists():
            history.replace(mind / "history.jsonl")
            for name in ("HISTORY.md", "HISTORY.md.bak"):
                if (ws / "memory" / name).is_file() and not (mind / name).exists():
                    (ws / "memory" / name).replace(mind / name)
            dream_cursor = hidden / ".dream_cursor"
            if dream_cursor.is_file():
                dream_cursor.replace(mind / ".dream_cursor")
            moved.append("history.jsonl")
        for name in (".cursor", ".dream_cursor"):
            (hidden / name).unlink(missing_ok=True)
        for empty in (hidden, ws / ".mira"):
            try:
                empty.rmdir()
            except OSError:
                pass
    if moved:
        logger.info("Moved long-term memory into {}: {}", mind, ", ".join(moved))
    return moved


class MemoryStore:
    """One memory layer: MEMORY.md (facts) + history.jsonl (grep-searchable log).

    The global layer is the assistant's mind in ``MIRA_HOME/mind``; only Dream
    edits its MEMORY.md, USER.md and SOUL.md. A project layer lives in the
    visible ``<project>/memory/`` folder and is maintained by the agent working
    on the project (consolidation is the safety net). Project history entries
    are mirrored into the mind's history so Dream learns from project work too.
    """

    def __init__(self, workspace: Path, max_history_entries: int = 1000, *, project: bool | None = None):
        from mira_engine.config.paths import get_mind_dir, get_workspace_path

        self.workspace = workspace
        self.max_history_entries = max_history_entries
        if project is None:
            project = workspace.resolve(strict=False) != get_workspace_path(None).resolve(strict=False)
        self.is_project = project
        self.mind_dir = get_mind_dir()
        if not project:
            migrate_to_mind_dir(workspace)
        self.memory_dir = ensure_dir(workspace / "memory") if project else self.mind_dir
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.history_jsonl_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.history_file
        self._legacy_history_backup_file = self.memory_dir / "HISTORY.md.bak"
        # Entries up to this cursor are folded into MEMORY.md (by Dream for the
        # mind, by consolidation for a project).
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self.soul_file = self.mind_dir / "SOUL.md"
        self.user_file = self.mind_dir / "USER.md"
        self.global_memory_file = self.mind_dir / "MEMORY.md"
        self._mind_history_file = self.mind_dir / "history.jsonl"

        self._git = GitStore(self.mind_dir, tracked_files=list(_MIND_FILES))

        if project:
            self._migrate_hidden_project_memory()
        self._migrate_legacy_history_if_needed()

    @property
    def git(self) -> GitStore:
        return self._git

    def _migrate_hidden_project_memory(self) -> None:
        """Fold the old hidden ``<project>/.mira/memory`` copy into ``memory/``."""
        hidden = self.workspace / ".mira" / "memory"
        if not hidden.is_dir():
            return
        old = hidden / "MEMORY.md"
        # Older builds read the hidden copy first, so it is what the agent saw.
        if old.is_file():
            text = _read_text(old)
            if text.strip() and text != _read_text(self.memory_file):
                atomic_write_text(self.memory_file, text)
            old.unlink(missing_ok=True)
        cursor = hidden / ".dream_cursor"
        if cursor.is_file() and not self._dream_cursor_file.exists():
            cursor.replace(self._dream_cursor_file)
        (hidden / ".cursor").unlink(missing_ok=True)
        cursor.unlink(missing_ok=True)
        try:
            hidden.rmdir()
        except OSError:
            pass

    def read_long_term(self) -> str:
        return _read_text(self.memory_file)

    def read_global_term(self) -> str:
        """The mind's MEMORY.md as seen from a project; empty for the mind itself."""
        return _read_text(self.global_memory_file) if self.is_project else ""

    def write_long_term(self, content: str) -> None:
        locked_write_text(self.memory_file, content)

    def ensure_mind_files(self) -> None:
        """Seed empty mind files so Dream's surgical edits have text to anchor on."""
        seeds = {
            "SOUL.md": _builtin_template("SOUL.md"),
            "USER.md": _builtin_template("USER.md"),
            "MEMORY.md": _MIND_MEMORY_SEED,
        }
        for name, seed in seeds.items():
            path = self.mind_dir / name
            if not _read_text(path).strip():
                atomic_write_text(path, seed)

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
        locked_write_text(self.soul_file, content)

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        locked_write_text(self.user_file, content)

    def append_history(self, entry: str) -> int:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        cleaned = strip_think(entry.rstrip()) or entry.rstrip()
        cursor = self._append_entry(self.history_jsonl_file, self._dream_cursor_file, ts, cleaned)
        if self.is_project and not cleaned.startswith("[RAW]"):
            # Dream reads only the mind's history; give it the project's story too.
            self._append_entry(
                self._mind_history_file, self.mind_dir / ".dream_cursor", ts, f"[project {self.workspace.name}] {cleaned}",
            )
        return cursor

    @classmethod
    def _append_entry(cls, history: Path, dream_cursor: Path, ts: str, content: str) -> int:
        # The cursor read plus the append must be atomic across processes:
        # concurrent instances sharing this file would otherwise mint duplicate
        # cursors and interleave partial lines.
        history.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_lock(history):
            # Never reuse a cursor Dream already passed, even if the log was trimmed by hand.
            cursor = max(cls._last_cursor(history), cls._read_cursor(dream_cursor)) + 1
            with open(history, "a", encoding="utf-8") as f:
                f.write(json.dumps({"cursor": cursor, "timestamp": ts, "content": content}, ensure_ascii=False) + "\n")
        return cursor

    @staticmethod
    def _read_cursor(path: Path) -> int:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            return 0

    @staticmethod
    def _row_cursor(row: Any) -> int | None:
        """A row's cursor, or None for rows not written by append_history (e.g. by hand)."""
        cursor = row.get("cursor") if isinstance(row, dict) else None
        return cursor if isinstance(cursor, int) and not isinstance(cursor, bool) else None

    @classmethod
    def _last_cursor(cls, history: Path) -> int:
        try:
            with open(history, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", errors="replace").splitlines()
                if size > 4096:
                    tail = tail[1:]  # the first line may be cut mid-way
        except FileNotFoundError:
            return 0
        for line in reversed(tail):
            try:
                cursor = cls._row_cursor(json.loads(line))
            except json.JSONDecodeError:
                continue
            if cursor is not None:
                return cursor
        # The tail held no valid cursor (e.g. rows appended by hand): scan it all.
        return max((r["cursor"] for r in cls._read_rows(history)), default=0)

    @classmethod
    def _read_rows(cls, source: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
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
                    if cls._row_cursor(row) is not None:
                        rows.append(row)
        except FileNotFoundError:
            pass
        return rows

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        self._migrate_legacy_history_if_needed()
        source = self.history_jsonl_file
        if not source.exists() and self.history_file.exists():
            source = self.history_file
        return [row for row in self._read_rows(source) if row["cursor"] > since_cursor]

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
            self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

        self.legacy_history_file.unlink(missing_ok=True)

    def get_last_dream_cursor(self) -> int:
        return self._read_cursor(self._dream_cursor_file)

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

    def commit_consolidated_memory(self, *, expected: str, update: str | None) -> bool:
        """Write the project MEMORY.md only if nobody changed it since ``expected`` was read."""
        if update is None or update == expected:
            return True
        with interprocess_lock(self.memory_file):
            if self.read_long_term() != expected:
                logger.warning("Memory consolidation skipped because MEMORY.md changed meanwhile")
                return False
            atomic_write_text(self.memory_file, update)
        return True

    def get_memory_context(self) -> str:
        global_term = self.read_global_term().strip()
        local_term = self.read_long_term().strip()
        if _is_memory_scaffold(global_term):
            global_term = ""
        if _is_memory_scaffold(local_term):
            local_term = ""
        if not self.is_project:
            return f"## Long-term Memory\n{local_term}" if local_term else ""
        parts = []
        if global_term:
            parts.append(f"## Long-term Memory\n{global_term}")
        if local_term:
            parts.append(f"## Project Memory\n{local_term}")
        return "\n\n".join(parts)

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

        if not any(k in args for k in ("history_entry", "project_memory_update")):
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
        """Fold old messages into history (and, for a project, its MEMORY.md) via an LLM tool call.

        The mind's MEMORY.md is never written here: Dream owns it and learns
        from the history entry. A project's consolidation also folds the
        project history entries archived since the last run, then marks them
        as folded so they leave the Recent History section.

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

        conversation = self._format_messages(old_messages)
        current_local = self.read_long_term()
        if self.is_project:
            pending = self.read_unprocessed_history(since_cursor=self.get_last_dream_cursor())
            pending_text = "\n".join(f"[{e.get('timestamp', '?')}] {e.get('content', '')}" for e in pending)
            prompt = f"""Consolidate this project conversation with the save_memory tool.
- history_entry: a short log entry of what happened.
- project_memory_update: the full updated project MEMORY.md. Keep what the project needs to move on
  (decisions, experiment results, data/file locations, pitfalls, current status); drop what is stale.
  Facts about the user in general are not project memory; they reach long-term memory through the history.

## Current Project Memory
{current_local or "(empty)"}

## Archived History Not Yet In Project Memory
{pending_text or "(none)"}

## Conversation to Process
{conversation}"""
        else:
            pending = []
            prompt = f"""Summarize this conversation for the history log with the save_memory tool.
history_entry: the key events, decisions, and facts learned about the user. Start with [YYYY-MM-DD HH:MM].

## Conversation to Process
{conversation}"""

        try:
            args: dict[str, Any] | None = None
            for attempt in range(1, _MAX_SAVE_MEMORY_ATTEMPTS + 1):
                response = await provider.chat(
                    messages=[
                        {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                        {"role": "user", "content": prompt},
                    ],
                    tools=_save_memory_tool(self.is_project),
                    tool_choice=_SAVE_MEMORY_TOOL_CHOICE,
                    model=model,
                )
                await record_token_usage(response.usage, model)
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

            project_update = args.get("project_memory_update") if self.is_project else None
            if project_update is not None and not isinstance(project_update, str):
                project_update = json.dumps(project_update, ensure_ascii=False)
            if not self.commit_consolidated_memory(expected=current_local, update=project_update):
                return False

            wrote_project = project_update is not None and project_update != current_local
            wrote_history = False
            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                cursor = self.append_history(entry)
                wrote_history = True
                if self.is_project:
                    # This entry and the pending ones were just folded into MEMORY.md.
                    self.set_last_dream_cursor(cursor)
            elif pending and self.is_project:
                self.set_last_dream_cursor(pending[-1]["cursor"])

            if archive_all:
                session.last_consolidated = 0
            else:
                boundary = len(session.messages) - keep_count
                # Align boundary to a user message so the unconsolidated
                # window never starts mid-tool-call-sequence.
                boundary = self._align_boundary_to_user(session.messages, boundary)
                session.last_consolidated = boundary
            logger.info("Memory consolidation writes: history={}, project={}", wrote_history, wrote_project)
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
            invoked_skills=invoked_skills_metadata(session.metadata),
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
            await record_token_usage(response.usage, self.model)
            self.store.append_history(response.content or "[no summary]")
            return True
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return True

    def _probe_upper_bound(self, session, budget: int) -> int | None:
        """Cheap upper bound on the prompt size, or None when a real probe is due.

        The exact probe builds the whole system prompt and runs the tokenizer
        over the full history before every turn. Once one exact estimate is
        known for a session, later turns only add a few messages; bounding
        their size by their character count (>= their token count) tells us
        whether the window can possibly be exceeded without rebuilding.
        """
        cache = getattr(self, "_probe_cache", None)
        if cache is None:
            cache = self._probe_cache = {}
        hit = cache.get(session.key)
        if hit is None:
            return None
        estimated, msg_count, consolidated = hit
        if consolidated != session.last_consolidated or len(session.messages) < msg_count:
            return None
        added = session.messages[msg_count:]
        try:
            added_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in added)
        except (TypeError, ValueError):
            return None
        return estimated + added_chars

    def _remember_probe(self, session, estimated: int) -> None:
        cache = getattr(self, "_probe_cache", None)
        if cache is None:
            cache = self._probe_cache = {}
        cache[session.key] = (estimated, len(session.messages), session.last_consolidated)

    async def maybe_consolidate_by_tokens(self, session) -> None:
        if not session.messages or self.context_window_tokens <= 0:
            return
        lock = self.get_lock(session.key)
        async with lock:
            budget = (
                self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
            )
            target = budget // 2
            upper = self._probe_upper_bound(session, budget)
            if upper is not None and upper < budget:
                return
            estimated, source = await asyncio.to_thread(
                self.estimate_session_prompt_tokens, session
            )
            if estimated > 0:
                self._remember_probe(session, estimated)
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
                estimated, source = await asyncio.to_thread(
                    self.estimate_session_prompt_tokens, session
                )
                if estimated <= 0:
                    return


class Dream:
    """Two-phase processor that folds the mind's unprocessed history into MEMORY.md, USER.md and SOUL.md."""

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
    ):
        if store.is_project:
            raise ValueError("Dream edits the mind in MIRA_HOME, not a project's memory")
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def _build_tools(self) -> ToolRegistry:
        from mira_engine.agent.tools.filesystem import EditFileTool, ReadFileTool

        tools = ToolRegistry()
        mind = self.store.mind_dir
        tools.register(ReadFileTool(workspace=mind, allowed_dir=mind))
        tools.register(EditFileTool(workspace=mind, allowed_dir=mind, protect_mind=False))
        return tools

    async def run(self) -> bool:
        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return False

        self.store.ensure_mind_files()
        self.store.git.init()

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
            await record_token_usage(phase1_response.usage, self.model)
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
        self.store.git.auto_commit(f"dream: {current_date}, history up to #{new_cursor}")
        return bool(result and result.stop_reason == "completed")
