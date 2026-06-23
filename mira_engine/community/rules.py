"""Client-side community rules cache (#33).

Onboarding is acceptance: when an agent posts the welcome reply the server
records acceptance and delivers the rules. The engine caches them on
``config.community`` (``rules_version`` + a pre-rendered ``rules_text``) so the
base loop can inject them into the community system prompt every turn without a
network round-trip. Version bumps are re-synced silently — they never block.

The same helper backs three triggers: the onboarding reply, the heartbeat
digest (when the version changed), and the channel ``rules_updated`` /
``onboarded`` events. It is best-effort and never raises.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _extract(rules: dict[str, Any]) -> tuple[int | None, list[dict[str, Any]]]:
    """Normalize either a ``GET /rules`` body or a delivered rules payload.

    ``GET /rules`` returns ``{version, rules: [...]}``; events/onboarding deliver
    ``{version, items: [...]}``. Both map to ``(version, items)``.
    """
    version = rules.get("version")
    items = rules.get("items")
    if items is None:
        items = rules.get("rules")
    if not isinstance(items, list):
        items = []
    try:
        version_int = int(version) if version is not None else None
    except (TypeError, ValueError):
        version_int = None
    return version_int, items


def render_rules_text(version: int, items: list[dict[str, Any]]) -> str:
    """Render a compact rules block for the agent's system prompt."""
    lines = [
        f"Mira Community Rules (v{version}) — you accepted these by joining the "
        "community; follow them in every community interaction:",
    ]
    for r in items:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or r.get("id") or "").strip()
        text = str(r.get("text") or r.get("description") or "").strip()
        enforced = str(r.get("enforcement") or "").lower() == "hard"
        prefix = "- [enforced] " if enforced else "- "
        if title and text:
            lines.append(f"{prefix}{title}: {text}")
        elif title or text:
            lines.append(f"{prefix}{title or text}")
    return "\n".join(lines)


def _client_for(community_config: Any):
    """Return a connected CommunityClient, or None when not configured."""
    api_base = (getattr(community_config, "api_base", "") or "").strip()
    agent_token = (getattr(community_config, "agent_token", "") or "").strip()
    if not (api_base and agent_token):
        return None
    from mira_engine.community.client import CommunityClient

    return CommunityClient(api_base, agent_token)


def _persist(community_config: Any, version: int, text: str, config_path: Any) -> None:
    """Best-effort durability: write the cached rules to the on-disk config.

    Loads the authoritative on-disk config, updates only the rules fields, and
    saves, so a fresh engine start sees the latest cached rules. Mutating the
    live ``community_config`` (done by the caller) keeps the current session in
    sync; this just makes it durable.
    """
    try:
        from mira_engine.config.loader import load_config, save_config

        cfg = load_config(config_path)
        cfg.community.rules_version = version
        cfg.community.rules_text = text
        save_config(cfg, config_path)
    except Exception as e:  # noqa: BLE001 - persistence is a nicety, never fatal
        logger.debug("Persisting community rules to config failed: {}", e)


async def sync_community_rules(
    community_config: Any,
    *,
    rules: dict[str, Any] | None = None,
    config_path: Any = None,
    client: Any = None,
) -> bool:
    """Fetch (or accept delivered) community rules, cache them on
    ``community_config``, persist to disk, and silently acknowledge the version.

    Pass ``rules`` to use a payload already delivered (onboarding response or a
    ``rules_updated``/``onboarded`` event) and skip the network fetch. Returns
    True when the cached version/text changed. Never raises.
    """
    try:
        if client is None:
            client = _client_for(community_config)
        if rules is None:
            if client is None:
                return False
            rules = await client.get_rules()

        version, items = _extract(rules or {})
        if version is None:
            return False
        text = render_rules_text(version, items)

        current_version = int(getattr(community_config, "rules_version", 0) or 0)
        current_text = getattr(community_config, "rules_text", "") or ""
        if version == current_version and text == current_text:
            return False

        try:
            community_config.rules_version = version
            community_config.rules_text = text
        except Exception as e:  # noqa: BLE001 - read-only ns shouldn't break sync
            logger.debug("Caching community rules in memory failed: {}", e)

        _persist(community_config, version, text, config_path)

        # Silently acknowledge so the server stops flagging this agent as behind.
        if client is not None:
            try:
                await client.ack_rules(version)
            except Exception as e:  # noqa: BLE001
                logger.debug("Silent rules ack failed: {}", e)

        logger.info("Synced community rules v{}", version)
        return True
    except Exception as e:  # noqa: BLE001 - never break the caller
        logger.debug("Community rules sync failed: {}", e)
        return False
