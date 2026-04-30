"""Session management for conversation history."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.config.paths import get_legacy_sessions_dir
from mira_engine.utils.helpers import ensure_dir, safe_filename, get_mira_dir

_EVENT_METADATA = "metadata"
_EVENT_MESSAGE = "message"
_EVENT_UI = "ui_event"
_EVENT_RESET = "session_reset"
_SESSION_EVENT_SCHEMA_VERSION = 2


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
    ui_events: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    _persisted_messages: int = 0
    _persisted_ui_events: int = 0
    _reset_pending: bool = False

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

    def add_ui_event(
        self,
        *,
        role: str,
        content: str,
        msg_type: str = "response",
        timestamp: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one UI-visible chat event to the session log."""
        event = {
            "role": role,
            "content": content,
            "type": msg_type,
            "timestamp": timestamp or datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self.ui_events.append(event)
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
            for k in ("tool_calls", "tool_call_id", "name", "reasoning_content"):
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
        # Only collapse when assistant turns exist. Pure user-only history should stay append-only.
        if any(m.get("role") == "assistant" for m in out):
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
            if msg["role"] == prev["role"] and msg["role"] in {"user", "assistant"}:
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
        self._reset_pending = True
        self._persisted_messages = 0
        self.updated_at = datetime.now()

    def retain_recent_legal_suffix(self, keep_count: int) -> None:
        """Keep a recent message suffix, aligned to a legal user-start boundary."""
        if keep_count <= 0:
            self.clear()
            return
        if keep_count >= len(self.messages):
            return

        start = len(self.messages) - keep_count
        if self.messages[start].get("role") != "user":
            forward_user = next(
                (i for i in range(start, len(self.messages)) if self.messages[i].get("role") == "user"),
                None,
            )
            if forward_user is not None:
                start = forward_user
            else:
                backward_user = next(
                    (i for i in range(start - 1, -1, -1) if self.messages[i].get("role") == "user"),
                    None,
                )
                if backward_user is not None:
                    start = backward_user

        if start <= 0:
            return

        self.messages = self.messages[start:]
        self.last_consolidated = max(0, self.last_consolidated - start)
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(get_mira_dir(self.workspace) / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.mira/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    @staticmethod
    def _session_metadata_event(session: Session) -> dict[str, Any]:
        return {
            "_type": _EVENT_METADATA,
            "schema_version": _SESSION_EVENT_SCHEMA_VERSION,
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "last_consolidated": session.last_consolidated,
        }

    @staticmethod
    def _message_event(session_key: str, message: dict[str, Any]) -> dict[str, Any]:
        return {
            "_type": _EVENT_MESSAGE,
            "schema_version": _SESSION_EVENT_SCHEMA_VERSION,
            "key": session_key,
            "message": message,
        }

    @staticmethod
    def _ui_event(session_key: str, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "_type": _EVENT_UI,
            "schema_version": _SESSION_EVENT_SCHEMA_VERSION,
            "key": session_key,
            "event": event,
        }

    @staticmethod
    def _reset_event(session_key: str, timestamp: str) -> dict[str, Any]:
        return {
            "_type": _EVENT_RESET,
            "schema_version": _SESSION_EVENT_SCHEMA_VERSION,
            "key": session_key,
            "timestamp": timestamp,
        }

    @staticmethod
    def _append_event(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

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

        if not path.exists() and key.startswith("ui:"):
            # Channel renamed from "web" to "ui" – pull forward any prior session
            # state stored under the legacy "web:" prefix so existing projects
            # keep their conversation history after upgrading.
            legacy_prefix_key = "web:" + key[len("ui:"):]
            legacy_prefix_path = self._get_session_path(legacy_prefix_key)
            if legacy_prefix_path.exists():
                try:
                    shutil.move(str(legacy_prefix_path), str(path))
                    logger.info(
                        "Migrated session {} from legacy 'web:' prefix at {}",
                        key,
                        legacy_prefix_path,
                    )
                except Exception:
                    logger.exception(
                        "Failed to migrate legacy 'web:' session for {}", key
                    )

        if not path.exists():
            return None

        try:
            messages = []
            ui_events = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)
                    event_type = data.get("_type")

                    # Backward-compatible metadata entries
                    if event_type == _EVENT_METADATA:
                        metadata = data.get("metadata", {})
                        if data.get("created_at"):
                            created_at = datetime.fromisoformat(data["created_at"])
                        if data.get("updated_at"):
                            updated_at = datetime.fromisoformat(data["updated_at"])
                        last_consolidated = int(data.get("last_consolidated", 0) or 0)
                        continue

                    # New append-only message envelope
                    if event_type == _EVENT_MESSAGE:
                        msg = data.get("message")
                        if isinstance(msg, dict):
                            messages.append(msg)
                        continue

                    # New append-only UI event envelope
                    if event_type == _EVENT_UI:
                        evt = data.get("event")
                        if isinstance(evt, dict):
                            ui_events.append(evt)
                        continue

                    # Logical reset marker for LLM context window
                    if event_type == _EVENT_RESET:
                        messages = []
                        last_consolidated = 0
                        continue

                    # Legacy raw message line format
                    if isinstance(data, dict) and data.get("role"):
                        messages.append(data)

            session = Session(
                key=key,
                messages=messages,
                ui_events=ui_events,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
            )
            session._persisted_messages = len(messages)
            session._persisted_ui_events = len(ui_events)
            return session
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Persist session changes in append-only event form."""
        path = self._get_session_path(session.key)
        if not path.exists():
            self._append_event(path, self._session_metadata_event(session))

        if session._reset_pending:
            self._append_event(path, self._reset_event(session.key, session.updated_at.isoformat()))
            session._reset_pending = False
            session._persisted_messages = 0

        new_messages = session.messages[session._persisted_messages:]
        for msg in new_messages:
            self._append_event(path, self._message_event(session.key, msg))
        session._persisted_messages = len(session.messages)

        new_ui_events = session.ui_events[session._persisted_ui_events:]
        for event in new_ui_events:
            self._append_event(path, self._ui_event(session.key, event))
        session._persisted_ui_events = len(session.ui_events)

        self._append_event(path, self._session_metadata_event(session))

        self._cache[session.key] = session

    def append_ui_event(
        self,
        *,
        key: str,
        role: str,
        content: str,
        msg_type: str = "response",
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Append one UI-visible event into the unified session event log."""
        session = self.get_or_create(key)
        session.add_ui_event(
            role=role,
            content=content,
            msg_type=msg_type,
            metadata=metadata,
            timestamp=timestamp,
        )
        self.save(session)

    def get_ui_history(self, key: str) -> list[dict[str, Any]]:
        """Build UI display entries from the unified event log."""
        session = self.get_or_create(key)
        entries: list[dict[str, Any]] = []

        for idx, event in enumerate(session.ui_events):
            role = event.get("role")
            if role not in {"user", "assistant"}:
                continue
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if role == "user":
                metadata = {**metadata, "_user": True}
            entry_type = event.get("type")
            if entry_type not in {"response", "progress", "tool_call", "error"}:
                entry_type = "response"
            entries.append({
                "id": f"ui-{safe_filename(key)}-{idx}",
                "timestamp": event.get("timestamp") or "",
                "content": event.get("content") or "",
                "type": entry_type,
                "metadata": metadata,
            })

        return [entry for entry in entries if entry["content"]]

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
                latest_meta: dict[str, Any] | None = None
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)
                        if data.get("_type") == _EVENT_METADATA:
                            latest_meta = data

                if latest_meta:
                    key = latest_meta.get("key") or path.stem.replace("_", ":", 1)
                    sessions.append({
                        "key": key,
                        "created_at": latest_meta.get("created_at"),
                        "updated_at": latest_meta.get("updated_at"),
                        "path": str(path),
                    })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
