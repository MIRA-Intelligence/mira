"""Community persona: what of the user's profile the agent may bring to the
community (design §11.3, mira#212).

Community turns never load ``USER.md`` (mira#203); they load
``COMMUNITY_PERSONA.md`` instead. This module drafts that file from the
private profile — keeping only community-relevant, non-identifying parts
(interests, languages, communication style) plus the public profile the user
already shared — and never writes it directly: a draft goes to the user's
approval queue as a diff, and only an approved draft becomes the persona.

Every drafted line is run through the privacy guard; anything that matches a
private term (name, emails, configured codenames, home paths) is dropped.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from mira_engine.community.privacy import COMMUNITY_PERSONA_FILE, PrivacyGuard

_SECTION = re.compile(r"^#{2,3}\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_LANGS = re.compile(r"\*\*Preferred language\(s\)\*\*:\s*(.+)")
_PLACEHOLDERS = {"not specified", ""}

# Sections of USER.md that carry community-relevant, non-identifying context.
_KEEP_SECTIONS = {"topics of interest": "Interests", "communication style": "Communication style"}


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            current = m.group(1).strip().lower()
            continue
        b = _BULLET.match(line)
        if current and b:
            out.setdefault(current, []).append(b.group(1))
    return out


def draft_persona(user_texts: list[str], profile: dict[str, Any] | None, guard: PrivacyGuard) -> str:
    """Draft COMMUNITY_PERSONA.md from the private profile + the public one."""
    profile = profile or {}
    lines = [
        "# Community persona",
        "",
        "How I present myself in the Mira community. Derived from my user's profile;",
        "approved by my user. Nothing here identifies them.",
        "",
    ]
    kept: dict[str, list[str]] = {}
    languages: list[str] = []
    for text in user_texts:
        for name, bullets in _sections(text).items():
            label = _KEEP_SECTIONS.get(name)
            if label:
                kept.setdefault(label, []).extend(bullets)
        for m in _LANGS.finditer(text):
            value = m.group(1).strip()
            if value.lower() not in _PLACEHOLDERS:
                languages.append(value)

    public = [
        ("Domains", ", ".join(profile.get("domains") or [])),
        ("Tech", ", ".join(profile.get("tech_stack") or [])),
        ("Languages", ", ".join(profile.get("languages") or []) or ", ".join(dict.fromkeys(languages))),
        ("About", profile.get("bio") or ""),
    ]
    for label, value in public:
        if value and not guard.scan(value):
            lines.append(f"- **{label}**: {value}")
    for label, bullets in kept.items():
        safe = [b for b in dict.fromkeys(bullets) if not guard.scan(b)]
        if safe:
            lines += ["", f"## {label}", "", *[f"- {b}" for b in safe]]
    return "\n".join(lines).rstrip() + "\n"


def persona_path(workspace: Path) -> Path:
    return workspace / COMMUNITY_PERSONA_FILE


def persona_diff(current: str, draft: str) -> str:
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            draft.splitlines(keepends=True),
            fromfile="COMMUNITY_PERSONA.md (current)",
            tofile="COMMUNITY_PERSONA.md (proposed)",
        )
    )


def propose_persona_update(workspace: Path, profile: dict[str, Any] | None, guard: PrivacyGuard) -> str | None:
    """Queue a persona change for the user's approval when the draft differs
    from the approved persona and isn't already pending. Returns the approval
    id, or None when there is nothing to ask."""
    from mira_engine.community import approvals

    texts = []
    for name in ("USER.md", "USER.local.md"):
        try:
            texts.append((workspace / name).read_text(encoding="utf-8"))
        except OSError:
            continue
    draft = draft_persona(texts, profile, guard)
    try:
        current = persona_path(workspace).read_text(encoding="utf-8")
    except OSError:
        current = ""
    if draft == current:
        return None
    for rec in approvals.list_approvals(status="pending"):
        if rec.get("action") == "persona_update" and (rec.get("payload") or {}).get("draft") == draft:
            return None
    return approvals.append_approval(
        "persona_update",
        {"workspace": str(workspace), "draft": draft, "diff": persona_diff(current, draft)},
    )


def apply_persona(workspace: Path, draft: str) -> Path:
    """Write an approved draft as the active persona."""
    path = persona_path(workspace)
    path.write_text(draft, encoding="utf-8")
    return path
