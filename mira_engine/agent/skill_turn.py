"""Per-turn skill handling shared by the base and research agent loops.

Skill bodies never go into the system prompt per turn. Instead:
- skills the user selected in the UI are loaded through a synthetic `skill`
  tool call appended after the user message, so the body enters the
  conversation once and is persisted with the session;
- keyword routing only names likely-relevant skills in the runtime context;
  the model decides whether to call the `skill` tool.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from mira_engine.agent.skills import SKILL_BASE_DIR_PREFIX, SKILL_TOOL_NAME, SkillsLoader
from mira_engine.utils.helpers import build_assistant_message

MAX_SELECTED_SKILLS = 8
MAX_INVOKED_SKILLS = 10


@dataclass
class TurnSkills:
    selected: list[str] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    suggested: list[str] = field(default_factory=list)
    mandatory_hint: str = ""
    synthetic_messages: list[dict[str, Any]] = field(default_factory=list)


def _call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def invoked_skill_names(messages: list[dict[str, Any]]) -> list[str]:
    """Skills whose `skill` tool call succeeded in ``messages``, in call order."""
    loaded_ids = {
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool"
        and m.get("name") == SKILL_TOOL_NAME
        and isinstance(m.get("content"), str)
        and m["content"].startswith(SKILL_BASE_DIR_PREFIX)
    }
    names: list[str] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            if (call.get("function") or {}).get("name") != SKILL_TOOL_NAME or call.get("id") not in loaded_ids:
                continue
            name = str(_call_arguments(call).get("skill") or "").strip().lstrip("/")
            if name:
                names.append(name)
    return names


def synthetic_skill_messages(skills: SkillsLoader, names: list[str]) -> list[dict[str, Any]]:
    """An assistant `skill` call plus its results, as if the model loaded ``names`` itself."""
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for name in names:
        content = skills.render_skill(name)
        if content is None:
            continue
        call_id = uuid.uuid4().hex[:9]
        calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": SKILL_TOOL_NAME, "arguments": json.dumps({"skill": name}, ensure_ascii=False)},
        })
        results.append({"role": "tool", "tool_call_id": call_id, "name": SKILL_TOOL_NAME, "content": content})
    if not calls:
        return []
    return [build_assistant_message(None, tool_calls=calls), *results]


def prepare_turn_skills(
    skills: SkillsLoader,
    meta: dict[str, Any],
    content: str,
    *,
    recent: list[str],
    history: list[dict[str, Any]],
) -> TurnSkills:
    raw_selected = meta.get("selected_skill_ids")
    selected: list[str] = []
    if isinstance(raw_selected, list):
        for item in raw_selected:
            name = item.strip() if isinstance(item, str) else ""
            if name and name not in selected and skills.load_skill(name):
                selected.append(name)
    selected = selected[:MAX_SELECTED_SKILLS]

    in_context = set(invoked_skill_names(history))
    injected = [name for name in selected if name not in in_context]
    synthetic = synthetic_skill_messages(skills, injected)
    injected = invoked_skill_names(synthetic)

    suggested = [
        name for name in skills.suggest_skills(content, recent=recent, limit=3)
        if name not in in_context and name not in selected and skills.is_model_invocable(name)
    ]

    mandatory_hint = ""
    if selected:
        fallback_instruction = (
            "The user approved the previously proposed fallback for this turn. "
            "Deviate only as narrowly as that proposal requires."
            if meta.get("_skill_fallback_approved")
            else "If their workflow cannot continue, stop using tools and reply with "
                 "`MIRA_SKILL_FALLBACK_REQUIRED:` followed by the exact error and proposed "
                 "fallback. Wait for explicit user approval before deviating."
        )
        mandatory_hint = (
            "User-selected skills are mandatory system-level instructions for this turn:\n"
            + "\n".join(f"- {name}" for name in selected)
            + "\nTheir full instructions are loaded in this conversation by the `skill` tool. "
              "Follow their documented workflow and recommended calls. Do not bypass them "
              "by inspecting implementation source, calling internal functions, or writing a "
              "replacement implementation. " + fallback_instruction
        )
    return TurnSkills(
        selected=selected,
        injected=injected,
        suggested=suggested,
        mandatory_hint=mandatory_hint,
        synthetic_messages=synthetic,
    )


def invoked_skills_metadata(session_metadata: Any) -> list[str]:
    raw = session_metadata.get("_invoked_skills") if isinstance(session_metadata, dict) else None
    return [str(name) for name in raw if isinstance(name, str)] if isinstance(raw, list) else []


def record_invoked_skills(session_metadata: Any, messages: list[dict[str, Any]]) -> None:
    """Remember skills loaded this turn (most recent last) for re-attachment after compaction."""
    if not isinstance(session_metadata, dict):
        return
    names = invoked_skill_names(messages)
    if not names:
        return
    merged = [name for name in invoked_skills_metadata(session_metadata) if name not in names]
    for name in names:
        if name in merged:
            merged.remove(name)
        merged.append(name)
    session_metadata["_invoked_skills"] = merged[-MAX_INVOKED_SKILLS:]
