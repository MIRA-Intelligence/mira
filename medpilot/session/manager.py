"""Session management for conversation history."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from medpilot.config.paths import get_legacy_sessions_dir
from medpilot.utils.helpers import ensure_dir, safe_filename, get_medpilot_dir


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn.

        Guarantees:
        - Starts with a user message.
        - No consecutive same-role messages.
        - Every assistant with tool_calls has ALL matching tool results.
        - No orphaned tool results.
        """
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks.
        found_user = False
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                found_user = True
                break
        if not found_user:
            return []

        # --- Pass 1: collect entries and track tool-call linkage ---
        out: list[dict[str, Any]] = []
        pending_tool_calls: set[str] = set()
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]

            if entry["role"] == "assistant":
                pending_tool_calls = {
                    tc.get("id")
                    for tc in entry.get("tool_calls", [])
                    if isinstance(tc, dict) and tc.get("id")
                }
                out.append(entry)
                continue

            if entry["role"] == "tool":
                tool_call_id = entry.get("tool_call_id")
                if not tool_call_id or tool_call_id not in pending_tool_calls:
                    continue
                pending_tool_calls.discard(tool_call_id)
                out.append(entry)
                continue

            if entry["role"] == "user":
                pending_tool_calls.clear()
            out.append(entry)

        # --- Pass 2: strip assistant tool_calls that lost any results ---
        out = self._strip_incomplete_tool_calls(out)

        # --- Pass 3: collapse consecutive same-role messages ---
        out = self._collapse_consecutive_roles(out)

        return out

    @staticmethod
    def _strip_incomplete_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove tool_calls from assistant messages whose results are incomplete."""
        result: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                expected_ids = {
                    tc.get("id")
                    for tc in msg["tool_calls"]
                    if isinstance(tc, dict) and tc.get("id")
                }
                # Collect immediately following tool results
                j = i + 1
                found_ids: set[str] = set()
                while j < len(messages) and messages[j].get("role") == "tool":
                    tid = messages[j].get("tool_call_id")
                    if tid in expected_ids:
                        found_ids.add(tid)
                    j += 1

                if found_ids == expected_ids:
                    result.append(msg)
                else:
                    # Drop tool_calls and keep only text content (if any).
                    # Also skip the orphaned tool results.
                    content = msg.get("content")
                    if content:
                        result.append({"role": "assistant", "content": content})
                    i = j  # skip past the orphaned tool results
                    continue
            else:
                result.append(msg)
            i += 1
        return result

    @staticmethod
    def _collapse_consecutive_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge consecutive same-role messages that providers reject."""
        if not messages:
            return messages
        result: list[dict[str, Any]] = [messages[0]]
        for msg in messages[1:]:
            prev = result[-1]
            if msg["role"] == prev["role"]:
                # Merge: keep the later message's content; skip if empty.
                prev_content = prev.get("content") or ""
                curr_content = msg.get("content") or ""
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    merged = (prev_content + "\n\n" + curr_content).strip()
                    prev["content"] = merged or prev_content or curr_content
                else:
                    prev["content"] = curr_content or prev_content
                # Preserve tool_calls from the later message if present.
                if msg.get("tool_calls"):
                    prev["tool_calls"] = msg["tool_calls"]
                if msg.get("tool_call_id"):
                    prev["tool_call_id"] = msg["tool_call_id"]
                if msg.get("name"):
                    prev["name"] = msg["name"]
            else:
                result.append(msg)
        return result

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(get_medpilot_dir(self.workspace) / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.medpilot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
