"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mira_engine.agent.memory import MemoryStore
from mira_engine.agent.skills import SkillsLoader
from mira_engine.utils.helpers import detect_image_mime
from mira_engine.utils.prompt_templates import render_template


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50

    def __init__(self, workspace: Path):
        from mira_engine.utils.helpers import get_mira_dir
        
        self.workspace = workspace
        self.mira_dir = get_mira_dir(workspace)
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        agents_filename: str = "AGENTS.md",
        channel: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files(agents_filename=agents_filename)
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary).strip())

        recent_history = self._build_recent_history_section()
        if recent_history:
            parts.append(recent_history)

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.mira_dir.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""

        return render_template(
            "agent/identity.md",
            runtime=runtime,
            workspace_path=workspace_path,
            platform_policy=platform_policy.strip(),
            channel=channel,
        ).strip()

    def _build_recent_history_section(self) -> str:
        """Build unprocessed history section for prompt cache continuity."""
        since_cursor = self.memory.get_last_dream_cursor()
        entries = self.memory.read_unprocessed_history(since_cursor=since_cursor)
        if not entries:
            return ""
        tail = entries[-self._MAX_RECENT_HISTORY:]
        lines = ["# Recent History"]
        for row in tail:
            ts = str(row.get("timestamp", "")).strip()
            content = str(row.get("content", "")).strip()
            if not content:
                continue
            if ts:
                lines.append(f"[{ts}] {content}")
            else:
                lines.append(content)
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        project_dir: str | None = None,
        run_mode: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
            if project_dir:
                lines.append(f"Project Directory: {project_dir}")
            elif channel == "web":
                lines.append(f"Project Directory: projects/{chat_id}")
            if run_mode:
                lines.append(f"Run Mode: {run_mode}")
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    @staticmethod
    def _load_builtin_template(filename: str) -> str | None:
        """Load a built-in template from the mira package."""
        from importlib.resources import files as pkg_files

        try:
            tpl_file = pkg_files("mira_engine") / "templates" / filename
            if tpl_file.is_file():
                return tpl_file.read_text(encoding="utf-8")
        except Exception:
            pass
        return None

    def _load_bootstrap_files(self, agents_filename: str = "AGENTS.md") -> str:
        """Load bootstrap files with override / append / fallback resolution.

        Per file (e.g. AGENTS.md):
          1. workspace/AGENTS.md exists  →  use it              (override)
          2. else                        →  built-in template   (fallback)
          3. workspace/AGENTS.local.md   →  append to base      (append)
        """
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            effective_name = agents_filename if filename == "AGENTS.md" else filename
            stem = effective_name.rsplit(".", 1)[0]

            ws_file = self.workspace / effective_name
            if ws_file.exists():
                content = ws_file.read_text(encoding="utf-8")
            else:
                content = self._load_builtin_template(effective_name) or ""

            if not content.strip():
                continue

            local_file = self.workspace / f"{stem}.local.md"
            if local_file.exists():
                extra = local_file.read_text(encoding="utf-8")
                if extra.strip():
                    content = content.rstrip() + "\n\n" + extra

            parts.append(f"## {effective_name}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _sanitize_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Last-resort guard: strip assistant tool_calls that lack matching tool_results.

        Prevents 400 errors from providers that strictly require every tool_use
        to be immediately followed by its tool_result.
        """
        result: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                expected = {
                    tc["id"]
                    for tc in msg["tool_calls"]
                    if isinstance(tc, dict) and tc.get("id")
                }
                j = i + 1
                found: set[str] = set()
                tool_msgs: list[dict[str, Any]] = []
                while j < len(messages) and messages[j].get("role") == "tool":
                    tid = messages[j].get("tool_call_id")
                    if tid in expected:
                        found.add(tid)
                        tool_msgs.append(messages[j])
                    j += 1

                if found == expected and found:
                    result.append(msg)
                    result.extend(tool_msgs)
                else:
                    content = msg.get("content")
                    if content:
                        result.append({"role": "assistant", "content": content})
                i = j if j > i + 1 else i + 1
            else:
                result.append(msg)
                i += 1
        return result

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        project_dir: str | None = None,
        run_mode: str | None = None,
        agents_filename: str = "AGENTS.md",
        extra_system: str | None = None,
        current_role: str = "user",
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id, project_dir, run_mode)
        user_content = self._build_user_content(current_message, media)

        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        history_copy = [dict(m) for m in history]
        if current_role == "assistant" and history_copy and history_copy[-1].get("role") == "assistant":
            prev = dict(history_copy[-1])
            prev_content = prev.get("content") or ""
            if isinstance(prev_content, str):
                prev["content"] = f"{prev_content}\n\n{current_message}".strip()
            else:
                prev["content"] = current_message
            history_copy[-1] = prev
            current_role = "user"

        system_prompt = self.build_system_prompt(
            skill_names,
            agents_filename=agents_filename,
            channel=channel,
        )
        if extra_system:
            system_prompt += "\n\n---\n\n" + extra_system

        return self._sanitize_tool_pairs([
            {"role": "system", "content": system_prompt},
            *history_copy,
            {"role": current_role, "content": merged},
        ])

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages
