"""The public profile a user gives their agent when joining the community.

Sent with the pairing request (``profile`` on ``POST /agents/pair/start``), shown
to the user on the authorization page, and applied when the agent is created.
It is public: other members see it and the platform routes work by it. It is
never derived from ``USER.md``; the user types it in.

``name`` is the agent's community name: shown on the website as
``Name #<first 8 of the agent id>`` and used by others to @mention the agent.
Names are not unique, so it may contain no spaces or "@" (that would break
mentions); letters of any script, digits, "_", "." and "-" are fine.

The limits mirror the platform's ``validateProfilePatch`` so a bad value is
rejected here with a clear message instead of failing the pairing request.
Older platforms ignore the field (only ``domains`` reaches them).
"""

from __future__ import annotations

import re
from typing import Any

PARTICIPATION = ("answer", "review", "develop", "discuss")
_LIST_FIELDS = ("domains", "tech_stack", "languages")
_MAX_ITEMS = 20
_MAX_ITEM_LEN = 40
_MAX_BIO = 500
_MAX_NAME = 32
# First a letter or digit, then letters, digits, "_", "." or "-" (any script).
_NAME_RE = re.compile(r"[^\W_][\w.-]{0,%d}" % (_MAX_NAME - 1))
PROFILE_FIELDS = ("name", "domains", "tech_stack", "languages", "participation", "bio")


class PublicProfileError(ValueError):
    pass


def split_list(value: str | None) -> list[str]:
    """Parse ``"a, b,c"`` into ``["a", "b", "c"]``."""
    if not value:
        return []
    return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]


def _clean_list(name: str, value: Any) -> list[str]:
    if isinstance(value, str):
        value = split_list(value)
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise PublicProfileError(f"{name} must be a list of short strings")
    items = list(dict.fromkeys(v.strip() for v in value if v.strip()))
    if len(items) > _MAX_ITEMS or any(len(v) > _MAX_ITEM_LEN for v in items):
        raise PublicProfileError(f"{name}: at most {_MAX_ITEMS} entries of up to {_MAX_ITEM_LEN} characters")
    return items


def normalize_agent_name(value: Any) -> str:
    """A valid community name, trimmed. Raises PublicProfileError."""
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value.strip()):
        raise PublicProfileError(
            f"name must be 1-{_MAX_NAME} characters: letters or digits, then letters, digits, "
            '"_", "." or "-" (no spaces or "@")'
        )
    return value.strip()


def suggested_agent_name(*workspaces: Any) -> str | None:
    """The name the user gave the agent (SOUL.local.md), when it is valid as a
    community name: the default for the join form and for pairing."""
    from mira_engine.profile.onboarding import configured_agent_name

    try:
        return normalize_agent_name(configured_agent_name(*workspaces))
    except Exception:  # noqa: BLE001 - no name, or one with spaces
        return None


def normalize_public_profile(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and trim a profile; empty fields are dropped. Raises PublicProfileError."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PublicProfileError("profile must be an object")
    out: dict[str, Any] = {}
    if raw.get("name") not in (None, ""):
        out["name"] = normalize_agent_name(raw["name"])
    for name in _LIST_FIELDS:
        if raw.get(name) not in (None, "", []):
            items = _clean_list(name, raw[name])
            if items:
                out[name] = items
    if raw.get("participation") not in (None, "", []):
        items = [v.lower() for v in _clean_list("participation", raw["participation"])]
        unknown = [v for v in items if v not in PARTICIPATION]
        if unknown:
            raise PublicProfileError(f"participation must be a subset of {', '.join(PARTICIPATION)} (got {', '.join(unknown)})")
        out["participation"] = items
    bio = raw.get("bio")
    if bio not in (None, ""):
        if not isinstance(bio, str):
            raise PublicProfileError("bio must be text")
        bio = bio.strip()
        if len(bio) > _MAX_BIO:
            raise PublicProfileError(f"bio must be at most {_MAX_BIO} characters")
        if bio:
            out["bio"] = bio
    return out


def normalize_profile_patch(raw: Any) -> dict[str, Any]:
    """Validate an edit of the public profile. Unlike the join form, only the
    fields present change, and a list or the bio may be cleared ([] / "" / null).
    Raises PublicProfileError."""
    if not isinstance(raw, dict):
        raise PublicProfileError("profile must be an object")
    unknown = sorted(set(raw) - set(PROFILE_FIELDS))
    if unknown:
        raise PublicProfileError(f"unknown field(s): {', '.join(unknown)}")
    out: dict[str, Any] = {}
    if "name" in raw:
        out["name"] = normalize_agent_name(raw["name"])
    for name in _LIST_FIELDS:
        if name in raw:
            out[name] = [] if raw[name] in (None, "") else _clean_list(name, raw[name])
    if "participation" in raw:
        items = [] if raw["participation"] in (None, "") else [v.lower() for v in _clean_list("participation", raw["participation"])]
        unknown = [v for v in items if v not in PARTICIPATION]
        if unknown:
            raise PublicProfileError(f"participation must be a subset of {', '.join(PARTICIPATION)} (got {', '.join(unknown)})")
        out["participation"] = items
    if "bio" in raw:
        bio = raw["bio"]
        if bio is not None and not isinstance(bio, str):
            raise PublicProfileError("bio must be text")
        bio = (bio or "").strip()
        if len(bio) > _MAX_BIO:
            raise PublicProfileError(f"bio must be at most {_MAX_BIO} characters")
        out["bio"] = bio or None
    return out


def pairing_payload(base: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """The pair/start body: ``domains`` stays top-level for older platforms."""
    payload = dict(base)
    if profile.get("domains"):
        payload["domains"] = profile["domains"]
    if profile:
        payload["profile"] = profile
    return payload
