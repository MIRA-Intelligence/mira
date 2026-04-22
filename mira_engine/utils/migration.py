"""One-time migrations from the legacy MedPilot layout to MIRA.

These run at CLI startup. They are idempotent: a marker file under the new
~/.mira directory prevents re-running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_MARKER_NAME = ".migrated-from-medpilot"


def migrate_legacy_home_dir() -> None:
    """Move ~/.medpilot/ to ~/.mira/ on first run, if present.

    - If ~/.mira already exists with the marker, do nothing.
    - If ~/.mira does not exist and ~/.medpilot does, rename the legacy dir.
    - If both exist, log a notice and leave both alone (user should merge
      manually to avoid silent data loss).
    """
    legacy = Path.home() / ".medpilot"
    target = Path.home() / ".mira"
    marker = target / _MARKER_NAME

    if target.exists() and marker.exists():
        return

    try:
        if legacy.exists() and not target.exists():
            legacy.rename(target)
            _write_marker(target, "renamed ~/.medpilot to ~/.mira")
            print(
                "MIRA: migrated legacy data directory ~/.medpilot -> ~/.mira",
                file=sys.stderr,
            )
            return
        if legacy.exists() and target.exists():
            print(
                "MIRA: both ~/.medpilot and ~/.mira exist; skipping auto-migration. "
                "Please merge them manually (prefer ~/.mira going forward).",
                file=sys.stderr,
            )
            _write_marker(target, "both existed; user merge required")
            return
        target.mkdir(parents=True, exist_ok=True)
        _write_marker(target, "fresh install")
    except OSError as exc:
        # Never crash startup because of a best-effort migration.
        print(f"MIRA: home-dir migration skipped ({exc})", file=sys.stderr)


def apply_legacy_env_var_fallback() -> None:
    """Map legacy MEDPILOT_* env vars onto MIRA_* if the new names are unset.

    Emits a one-time deprecation warning for each mapped variable.
    """
    pairs = [
        ("MEDPILOT_CONFIG_PATH", "MIRA_CONFIG_PATH"),
        ("MEDPILOT_BRANCH", "MIRA_BRANCH"),
        ("MEDPILOT_RESTART_NOTIFY_CHANNEL", "MIRA_RESTART_NOTIFY_CHANNEL"),
        ("MEDPILOT_RESTART_NOTIFY_CHAT_ID", "MIRA_RESTART_NOTIFY_CHAT_ID"),
        ("MEDPILOT_RESTART_STARTED_AT", "MIRA_RESTART_STARTED_AT"),
        ("MEDPILOT_TMUX_SOCKET_DIR", "MIRA_TMUX_SOCKET_DIR"),
    ]
    warned = False
    for legacy, new in pairs:
        legacy_value = os.environ.get(legacy)
        if legacy_value and not os.environ.get(new):
            os.environ[new] = legacy_value
            if not warned:
                print(
                    "MIRA: detected legacy MEDPILOT_* environment variables; "
                    "they are being honored for now but please migrate to MIRA_*.",
                    file=sys.stderr,
                )
                warned = True


def run_startup_migrations() -> None:
    """Run all one-time migrations. Safe to call multiple times."""
    apply_legacy_env_var_fallback()
    migrate_legacy_home_dir()


def _write_marker(target: Path, reason: str) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / _MARKER_NAME).write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass
