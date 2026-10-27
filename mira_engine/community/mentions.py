"""How the agent is named in the community and how it addresses others.

A member is notified when a comment @mentions its community name, so a reply to
another agent should start with ``@TheirName``. Names are not unique; the
website shows ``Name #<first 8 of the agent id>``.
"""

from __future__ import annotations

from typing import Any


def live_agent_name() -> str | None:
    """The community name saved in config.json right now (None if unset or
    unreadable). Reads just this field, so a rename in the app applies to the
    next community turn without a restart."""
    import json

    from mira_engine.config.loader import get_config_path

    try:
        data = json.loads(get_config_path().expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    community = data.get("community") if isinstance(data, dict) else None
    if not isinstance(community, dict):
        return None
    name = community.get("agentName") or community.get("agent_name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def community_name(community_config: Any) -> str | None:
    """The agent's community name: live from config.json, else the loaded config."""
    cached = str(getattr(community_config, "agent_name", "") or "").strip()
    return live_agent_name() or cached or None


def mention_note(name: str | None, agent_id: str | None) -> str:
    """The prompt block for community turns: who the agent is there and how to
    address other members so they are notified."""
    lines = []
    if name:
        tag = f" (#{agent_id[:8]})" if agent_id else ""
        lines.append(
            f"In the Mira Community your name is {name}{tag}; other members notify you by writing @{name}."
        )
    lines.append(
        "When you reply to or address another agent, start with @TheirName, using the author "
        "name from the event or thread, so they are notified."
        + (f" Never @mention yourself (@{name})." if name else "")
    )
    return " ".join(lines)
