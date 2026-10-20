"""First-run profile onboarding (USER.local.md + SOUL.local.md).

Single source of truth for detecting, parsing, rendering, and writing the
user/agent profile. Both the desktop wizard (``POST /api/profile``) and the CLI
(``mira onboard`` / ``mira profile``) call these functions, so the two surfaces
never diverge.

Onboarding writes to the *append/overlay* files, not the base templates:
- User info -> ``USER.local.md`` (Basic Information: Name, Timezone, Preferred
  language(s); plus optional topics/special instructions).
- Agent info -> ``SOUL.local.md`` (agent name + identity; optional personality).

These ``*.local.md`` files are appended on top of the base ``USER.md`` /
``SOUL.md`` by :class:`mira_engine.agent.context.ContextBuilder`, so the bundled
templates stay generic and user customization lives in one place.

Timezone and language are also synced into ``config.agents.defaults`` so they
take effect (timezone drives cron/heartbeat/time context), and
``config.profile.onboarded`` is flipped so neither surface re-prompts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

USER_LOCAL_FILE = "USER.local.md"
SOUL_LOCAL_FILE = "SOUL.local.md"


def detect_timezone() -> str:
    """Best-effort IANA timezone of the host (e.g. ``Asia/Shanghai``).

    Used so the CLI never has to prompt for it. Falls back to the local
    ``tzinfo`` name and finally to ``UTC``.
    """
    try:
        from tzlocal import get_localzone_name

        name = get_localzone_name()
        if name:
            return name
    except Exception:  # noqa: BLE001 - optional dependency / detection failure
        pass
    try:
        import datetime

        tz = datetime.datetime.now().astimezone().tzinfo
        if tz is not None:
            return str(tz)
    except Exception:  # noqa: BLE001
        pass
    return "UTC"


def load_template(name: str) -> str:
    """Load a bundled workspace template by file name (e.g. ``USER.md``)."""
    from importlib.resources import files as pkg_files

    try:
        path = pkg_files("mira_engine") / "templates" / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - missing template is non-fatal
        pass
    return ""


def _workspace_of(config: Any, workspace: Path | None) -> Path:
    if workspace is not None:
        return Path(workspace)
    ws = getattr(config, "workspace_path", None)
    if ws is not None:
        return Path(ws)
    return Path("~/.mira/workspace").expanduser()


def _read(workspace: Path, name: str) -> str:
    try:
        return (workspace / name).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - missing file reads as empty
        return ""


def needs_onboarding(config: Any, workspace: Path | None = None) -> bool:
    """True when the profile has not been set up yet.

    Honours the persistent ``config.profile.onboarded`` flag first; otherwise
    prompts while either overlay file (``USER.local.md`` / ``SOUL.local.md``)
    is missing or empty.
    """
    if getattr(getattr(config, "profile", None), "onboarded", False):
        return False
    ws = _workspace_of(config, workspace)
    return not _read(ws, USER_LOCAL_FILE).strip() or not _read(ws, SOUL_LOCAL_FILE).strip()


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #


def _basic_field(text: str, label: str) -> str:
    m = re.search(rf"^-\s*\*\*{re.escape(label)}\*\*:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _parse_soul(text: str) -> tuple[str, str]:
    """Return (agent_name, identity) parsed from the ``I am ...`` line."""
    m = re.search(r"(?m)^I am\s+(.+)$", text)
    if not m:
        return "", ""
    rest = m.group(1).strip().rstrip(".")
    parts = rest.split(",", 1)
    name = parts[0].strip()
    identity = parts[1].strip() if len(parts) > 1 else ""
    return name, identity


def get_profile_state(config: Any, workspace: Path | None = None) -> dict[str, Any]:
    """Return current profile values for prefilling the wizard / prompts."""
    ws = _workspace_of(config, workspace)
    user_text = _read(ws, USER_LOCAL_FILE)
    soul_text = _read(ws, SOUL_LOCAL_FILE)

    defaults = getattr(getattr(config, "agents", None), "defaults", None)
    cfg_tz = (getattr(defaults, "timezone", "") or "").strip()
    cfg_lang = (getattr(defaults, "language", "") or "").strip()

    timezone = _basic_field(user_text, "Timezone") or (cfg_tz if cfg_tz != "UTC" else "")
    languages = _basic_field(user_text, "Preferred language(s)") or cfg_lang

    agent_name, identity = _parse_soul(soul_text)
    return {
        "needs_onboarding": needs_onboarding(config, ws),
        "user": {
            "name": _basic_field(user_text, "Name"),
            "timezone": timezone,
            "languages": languages,
        },
        "agent": {"name": agent_name, "identity": identity},
    }


# --------------------------------------------------------------------------- #
# Rendering (overlay files)                                                    #
# --------------------------------------------------------------------------- #


def _normalize_languages(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def render_user_local(fields: dict[str, Any]) -> str:
    """Render ``USER.local.md`` (Basic Information + optional sections)."""
    name = str(fields.get("name") or "").strip() or "MIRA User"
    tz = str(fields.get("timezone") or "").strip() or "Not specified"
    langs = _normalize_languages(fields.get("languages")) or "Not specified"

    lines = [
        "## Basic Information",
        "",
        f"- **Name**: {name}",
        f"- **Timezone**: {tz}",
        f"- **Preferred language(s)**: {langs}",
    ]

    topics = fields.get("topics")
    if topics:
        body = [f"- {str(t).strip()}" for t in topics if str(t).strip()]
        if body:
            lines += ["", "## Topics of Interest", "", *body]

    special = fields.get("special_instructions")
    if special:
        body = [f"- {str(s).strip()}" for s in special if str(s).strip()]
        if body:
            lines += ["", "## Special Instructions", "", *body]

    return "\n".join(lines) + "\n"


def render_soul_local(fields: dict[str, Any]) -> str:
    """Render ``SOUL.local.md`` (user-configured agent identity)."""
    name = str(fields.get("name") or "").strip()
    identity = str(fields.get("identity") or "").strip()

    line = f"I am {name}, {identity}." if (name and identity) else (f"I am {name}." if name else "")
    lines = [
        "## Identity (user-configured)",
        "",
        "This is the authoritative name and identity for this agent. It "
        "supersedes any default name in the base SOUL.md.",
    ]
    if line:
        lines += ["", line]

    personality = fields.get("personality")
    if personality:
        body = [f"- {str(p).strip()}" for p in personality if str(p).strip()]
        if body:
            lines += ["", "## Personality", "", *body]

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Apply                                                                        #
# --------------------------------------------------------------------------- #


def apply_profile(
    config: Any,
    payload: dict[str, Any],
    *,
    workspace: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Write the overlay files, sync timezone+language into config, set the flag.

    ``payload`` shape: ``{"user": {name, timezone, languages, ...},
    "agent": {name, identity, ...}}``. Returns ``{"ok": True, ...}``.
    """
    ws = _workspace_of(config, workspace)
    ws.mkdir(parents=True, exist_ok=True)

    user = dict(payload.get("user") or {})
    agent = dict(payload.get("agent") or {})

    if not (str(agent.get("name") or "").strip()):
        raise ValueError("agent name is required")

    languages = _normalize_languages(user.get("languages"))
    user["languages"] = languages

    (ws / USER_LOCAL_FILE).write_text(render_user_local(user), encoding="utf-8")
    (ws / SOUL_LOCAL_FILE).write_text(render_soul_local(agent), encoding="utf-8")

    timezone = str(user.get("timezone") or "").strip()
    if timezone:
        config.agents.defaults.timezone = timezone
    config.agents.defaults.language = languages
    config.profile.onboarded = True

    from mira_engine.config.loader import save_config

    save_config(config, config_path)
    return {"ok": True, "needs_onboarding": False}
