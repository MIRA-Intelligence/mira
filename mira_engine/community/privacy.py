"""Privacy boundary between the user's private context and the public community.

Two halves (design §11.3, mira#203):

* **Inbound** — community turns never see the private profile. The constants
  here tell :class:`~mira_engine.agent.context.ContextBuilder` which bootstrap
  files a community turn may load; ``USER.md`` (and its ``.local`` overlay),
  long-term memory and cross-session history are excluded.
* **Outbound** — everything the engine sends to the community is scanned
  before it leaves the machine: credentials always block; the user's private
  terms (name and email from the profile, OS username, home path, plus any
  ``community.private_terms`` from config), email addresses and home-directory
  paths are flagged so the agent can rewrite.

This is a code-level backstop, not a guarantee: free text can leak in ways no
pattern list catches. It exists so that the common, accidental leaks (a name,
a path, a token pasted from a log) never reach a public thread.
"""

from __future__ import annotations

import getpass
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

COMMUNITY_CHANNEL = "community"

# Bootstrap files a community turn may load. USER.md is deliberately absent.
COMMUNITY_BOOTSTRAP_FILES = ("AGENTS.md", "SOUL.md", "TOOLS.md")
# Optional, user-approved public persona for community turns (design §11.3).
COMMUNITY_PERSONA_FILE = "COMMUNITY_PERSONA.md"

# Profile names the onboarding writes when the user gave none.
_PLACEHOLDER_NAMES = {"mira user", "not specified", ""}

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("API key", re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|nvapi-[A-Za-z0-9_-]{20,})")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    (
        "credential assignment",
        re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    ),
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HOME_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)([^/\\\s]+)")
_PUBLIC_EMAIL_DOMAINS = ("noreply.github.com", "example.com", "example.org")
_PROFILE_NAME = re.compile(r"\*\*Name\*\*:\s*(.+)")


@dataclass(frozen=True)
class Finding:
    kind: str  # "secret" | "private-term" | "email" | "home-path"
    label: str


class PrivacyViolation(Exception):
    """Outbound community content was blocked by the privacy filter."""

    def __init__(self, findings: list[Finding]):
        self.findings = findings
        kinds = ", ".join(sorted({f.label for f in findings}))
        super().__init__(f"blocked by the community privacy filter: {kinds}")


class PrivacyGuard:
    """Scan / redact text bound for the community."""

    def __init__(self, private_terms: Iterable[str] = ()):
        terms = {t.strip() for t in private_terms if t and len(t.strip()) >= 3}
        # Longest first so a full name redacts before its parts.
        self._terms = sorted(terms, key=len, reverse=True)
        self._term_res = [re.compile(re.escape(t), re.IGNORECASE) for t in self._terms]

    @classmethod
    def from_workspace(cls, workspace: Path | None, extra_terms: Iterable[str] = ()) -> PrivacyGuard:
        return cls([*_profile_terms(workspace), *_os_terms(), *extra_terms])

    def scan(self, text: str, *, secrets_only: bool = False) -> list[Finding]:
        findings = [Finding("secret", label) for label, pat in _SECRET_PATTERNS if pat.search(text)]
        if secrets_only:
            return findings
        if any(pat.search(text) for pat in self._term_res):
            findings.append(Finding("private-term", "a private term from your profile/config"))
        if any(not _is_public_email(m.group(0)) for m in _EMAIL.finditer(text)):
            findings.append(Finding("email", "an email address"))
        if _HOME_PATH.search(text):
            findings.append(Finding("home-path", "a home-directory path"))
        return findings

    def redact(self, text: str) -> tuple[str, list[Finding]]:
        """Return ``text`` with private terms/emails/home paths masked. Secrets
        are reported but deliberately not "fixed": callers must drop them."""
        findings = self.scan(text)
        out = _HOME_PATH.sub("~", text)
        out = _EMAIL.sub(lambda m: m.group(0) if _is_public_email(m.group(0)) else "[email]", out)
        for pat in self._term_res:
            out = pat.sub("[redacted]", out)
        return out, findings


def _is_public_email(addr: str) -> bool:
    return addr.lower().endswith(_PUBLIC_EMAIL_DOMAINS)


def _profile_terms(workspace: Path | None) -> list[str]:
    """The user's name and email addresses from the private profile files."""
    if workspace is None:
        return []
    terms: list[str] = []
    for name in ("USER.md", "USER.local.md"):
        path = workspace / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _PROFILE_NAME.finditer(text):
            value = m.group(1).strip().strip("*").strip()
            if value.lower() not in _PLACEHOLDER_NAMES:
                terms.append(value)
        terms.extend(e for e in _EMAIL.findall(text) if not _is_public_email(e))
    return terms


def _os_terms() -> list[str]:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name in some sandboxes
        return []
    # Short or generic account names would flag ordinary words.
    return [user] if len(user) >= 4 and user not in {"root", "user", "admin", "runner"} else []


# Request fields that carry free text authored by the agent. ``diff`` is code:
# it is checked for secrets only (code legitimately contains emails/paths).
_TEXT_FIELDS = {"content", "body", "title", "text", "reason"}
_CODE_FIELDS = {"diff"}


def check_outbound_payload(guard: PrivacyGuard, payload: dict[str, Any]) -> None:
    """Raise :class:`PrivacyViolation` when an outbound community request would
    leak a secret or private detail. Scans nested string fields by name."""
    findings: list[Finding] = []

    def walk(value: Any, key: str | None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and key in _TEXT_FIELDS:
            findings.extend(guard.scan(value))
        elif isinstance(value, str) and key in _CODE_FIELDS:
            findings.extend(guard.scan(value, secrets_only=True))

    walk(payload, None)
    if findings:
        raise PrivacyViolation(findings)


def default_guard() -> PrivacyGuard:
    """Guard built from the configured workspace (where the user's profile
    lives) plus ``community.private_terms``. Falls back to OS-level terms when
    the config cannot be read, so the filter never silently switches off."""
    workspace: Path | None = None
    extra: list[str] = []
    try:
        from mira_engine.config.loader import load_config

        cfg = load_config()
        workspace = cfg.workspace_path
        extra = list(getattr(cfg.community, "private_terms", []) or [])
    except Exception:  # noqa: BLE001 - never block on config resolution
        pass
    return PrivacyGuard.from_workspace(workspace, extra)
