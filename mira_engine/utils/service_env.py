"""The Windows service's view of the user's profile.

The desktop bundle registers the engine as a Windows service through WinSW;
without a ``<serviceaccount>`` it runs as LocalSystem. ``install-service``
points ``HOME`` and ``USERPROFILE`` at the user's profile so the engine finds
the user's config and workspace, but ``APPDATA`` and ``LOCALAPPDATA`` still
name the system profile (``C:\\Windows\\system32\\config\\systemprofile\\
AppData\\…``). GitHub CLI keeps its config in ``%APPDATA%\\GitHub CLI``, so
``gh auth status`` inside the service looked at a profile where nobody had
ever logged in — while the user's terminal was fine — and the community
contribution flow stopped at "GitHub CLI is not logged in".

:func:`ensure_user_profile_dirs` runs when the gateway starts as a Windows
service and points ``APPDATA`` / ``LOCALAPPDATA`` at the user's profile
whenever they lie outside it. The token is a second hurdle: ``gh auth login``
stores it in the Windows Credential Manager of the account that logged in,
which LocalSystem cannot open; gh then falls back to the token in
``hosts.yml``, which only ``gh auth login --insecure-storage`` writes.
:func:`gh_login_hint` tells the user exactly that when the check fails.
"""

from __future__ import annotations

import ntpath
import os
import sys
from collections.abc import Mapping, MutableMapping

WINDOWS_SERVICE_MODE = "windows-service"

# Environment variable -> path under the user's profile.
PROFILE_DIRS: dict[str, tuple[str, ...]] = {
    "APPDATA": ("AppData", "Roaming"),
    "LOCALAPPDATA": ("AppData", "Local"),
}


def _runs_as_windows_service(
    platform: str, service_mode: str | None, environ: Mapping[str, str]
) -> bool:
    mode = service_mode if service_mode is not None else environ.get("MIRA_SERVICE_MODE", "")
    return platform == "win32" and mode == WINDOWS_SERVICE_MODE


def _inside(path: str, root: str) -> bool:
    """Whether a Windows ``path`` is ``root`` or lies under it (case-insensitive)."""
    p = ntpath.normcase(ntpath.normpath(path))
    r = ntpath.normcase(ntpath.normpath(root))
    return p == r or p.startswith(r.rstrip("\\") + "\\")


def profile_dirs(user_profile: str) -> dict[str, str]:
    """``APPDATA`` / ``LOCALAPPDATA`` as they are for the owner of ``user_profile``."""
    return {name: ntpath.join(user_profile, *parts) for name, parts in PROFILE_DIRS.items()}


def ensure_user_profile_dirs(
    environ: MutableMapping[str, str] = os.environ,
    *,
    platform: str = sys.platform,
    service_mode: str | None = None,
) -> dict[str, str]:
    """Point ``APPDATA`` / ``LOCALAPPDATA`` at the user's profile in a Windows service.

    Does nothing outside Windows service mode, without ``USERPROFILE``, or
    when the variables already lie under it (a service running as the user).
    Returns the variables it changed. Pure apart from the mapping, so the
    policy is testable on any platform.
    """
    if not _runs_as_windows_service(platform, service_mode, environ):
        return {}
    user_profile = environ.get("USERPROFILE", "")
    if not user_profile:
        return {}
    changed: dict[str, str] = {}
    for name, target in profile_dirs(user_profile).items():
        current = environ.get(name, "")
        if current and _inside(current, user_profile):
            continue
        environ[name] = target
        changed[name] = target
    return changed


def gh_login_hint(
    *,
    platform: str = sys.platform,
    service_mode: str | None = None,
    environ: Mapping[str, str] = os.environ,
) -> str:
    """What a Windows-service user must do for ``gh`` to be logged in here, else ''."""
    if not _runs_as_windows_service(platform, service_mode, environ):
        return ""
    return (
        "On Windows the engine runs as a system service (LocalSystem), which cannot open the "
        "Credential Manager vault where GitHub CLI keeps your token. Log in with "
        "`gh auth login --insecure-storage` so the token is kept in %APPDATA%\\GitHub CLI\\hosts.yml, "
        "then run `gh auth setup-git` so git pushes with it."
    )
