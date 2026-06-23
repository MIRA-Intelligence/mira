"""First-run profile onboarding (USER.md + SOUL.md).

Single source of truth for detecting, parsing, rendering, and writing the
user/agent profile. Both the desktop wizard (``POST /api/profile``) and the CLI
(``mira onboard``) call these functions, so the two surfaces never diverge.

Onboarding collects:
- User info -> ``USER.md`` Basic Information (Name, Timezone, Preferred
  language(s)); optional sections default to the bundled template text.
- Agent info -> ``SOUL.md`` (agent name required; identity/personality optional).

Timezone and language are also synced into ``config.agents.defaults`` so they
take effect (timezone drives cron/heartbeat/time context), and
``config.profile.onboarded`` is flipped so neither surface re-prompts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_USER_PLACEHOLDERS = {
    "Name": "MIRA User",
    "Timezone": "Not specified",
    "Preferred language(s)": "Not specified",
}


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


def _is_default_or_missing(workspace: Path, name: str) -> bool:
    current = _read(workspace, name).strip()
    if not current:
        return True
    return current == load_template(name).strip()


def needs_onboarding(config: Any, workspace: Path | None = None) -> bool:
    """True when the profile has not been set up yet.

    Honours the persistent ``config.profile.onboarded`` flag first; otherwise
    auto-prompts only while ``USER.md``/``SOUL.md`` are still the untouched
    default template (so manual edits are respected and never nagged).
    """
    if getattr(getattr(config, "profile", None), "onboarded", False):
        return False
    ws = _workspace_of(config, workspace)
    return _is_default_or_missing(ws, "USER.md") or _is_default_or_missing(ws, "SOUL.md")


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
    user_text = _read(ws, "USER.md") or load_template("USER.md")
    soul_text = _read(ws, "SOUL.md") or load_template("SOUL.md")

    def _clean(value: str, label: str) -> str:
        value = (value or "").strip()
        return "" if value == _USER_PLACEHOLDERS.get(label, "") else value

    agent_name, identity = _parse_soul(soul_text)
    return {
        "needs_onboarding": needs_onboarding(config, ws),
        "user": {
            "name": _clean(_basic_field(user_text, "Name"), "Name"),
            "timezone": _clean(_basic_field(user_text, "Timezone"), "Timezone"),
            "languages": _clean(
                _basic_field(user_text, "Preferred language(s)"), "Preferred language(s)"
            ),
        },
        "agent": {"name": agent_name, "identity": identity},
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #


def _set_basic(text: str, label: str, value: str) -> str:
    pattern = re.compile(rf"^(-\s*\*\*{re.escape(label)}\*\*:\s*).*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda m: m.group(1) + value, text, count=1)
    return text


def _replace_section(text: str, heading: str, body: str) -> str:
    """Replace the body under a markdown heading, keeping the heading itself."""
    pattern = re.compile(
        rf"(^{re.escape(heading)}[ \t]*\n)(.*?)(?=^#{{1,6}}[ \t]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    if not pattern.search(text):
        return text
    return pattern.sub(lambda m: m.group(1) + "\n" + body.rstrip() + "\n\n", text, count=1)


def _normalize_languages(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def render_user_md(fields: dict[str, Any]) -> str:
    """Render ``USER.md`` from the template with provided values substituted."""
    text = load_template("USER.md")
    name = (str(fields.get("name") or "")).strip() or _USER_PLACEHOLDERS["Name"]
    tz = (str(fields.get("timezone") or "")).strip() or _USER_PLACEHOLDERS["Timezone"]
    langs = _normalize_languages(fields.get("languages")) or _USER_PLACEHOLDERS[
        "Preferred language(s)"
    ]
    text = _set_basic(text, "Name", name)
    text = _set_basic(text, "Timezone", tz)
    text = _set_basic(text, "Preferred language(s)", langs)

    topics = fields.get("topics")
    if topics:
        body = "\n".join(f"- {str(t).strip()}" for t in topics if str(t).strip())
        if body:
            text = _replace_section(text, "## Topics of Interest", body)

    special = fields.get("special_instructions")
    if special:
        body = "\n".join(f"- {str(s).strip()}" for s in special if str(s).strip())
        if body:
            text = _replace_section(text, "## Special Instructions", body)

    return text


def render_soul_md(fields: dict[str, Any]) -> str:
    """Render ``SOUL.md`` from the template with the agent identity substituted."""
    text = load_template("SOUL.md")
    name = (str(fields.get("name") or "")).strip()
    identity = (str(fields.get("identity") or "")).strip()

    if name:
        _, base_identity = _parse_soul(text)
        desc = identity or base_identity
        line = f"I am {name}, {desc}." if desc else f"I am {name}."
        text = re.sub(r"(?m)^I am .*$", lambda _m: line, text, count=1)

    personality = fields.get("personality")
    if personality:
        body = "\n".join(f"- {str(p).strip()}" for p in personality if str(p).strip())
        if body:
            text = _replace_section(text, "## Personality", body)

    return text


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
    """Write USER.md/SOUL.md, sync timezone+language into config, set the flag.

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

    (ws / "USER.md").write_text(render_user_md(user), encoding="utf-8")
    (ws / "SOUL.md").write_text(render_soul_md(agent), encoding="utf-8")

    timezone = str(user.get("timezone") or "").strip()
    if timezone:
        config.agents.defaults.timezone = timezone
    config.agents.defaults.language = languages
    config.profile.onboarded = True

    from mira_engine.config.loader import save_config

    save_config(config, config_path)
    return {"ok": True, "needs_onboarding": False}
