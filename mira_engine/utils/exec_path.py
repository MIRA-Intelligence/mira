"""Programs the engine needs that a service process may not see.

The desktop bundle runs the engine as a launchd agent (macOS), a Windows
service or a systemd user unit. None of those start from the user's login
shell, so ``PATH`` is the system default — ``/usr/bin:/bin:/usr/sbin:/sbin``
on macOS — and whatever the user installed with Homebrew, MacPorts, pipx,
winget or scoop is invisible to the engine even though the same command
works in their terminal. ``gh`` was the first casualty: the community
contribution flow runs ``gh auth status`` and got ``FileNotFoundError``
on a Mac where ``gh`` sat in ``/opt/homebrew/bin``.

:func:`ensure_service_path` runs when the gateway starts as a service and
appends the well-known install directories that exist on this machine to
``PATH``. They go *after* the current entries, so nothing the user already
had on ``PATH`` changes meaning; programs that were missing become found.
:func:`resolve_executable` finds a program on that extended ``PATH`` and
:func:`missing_executable_message` says what was searched when it isn't
there, so the agent reports an installation problem instead of a traceback.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path


def well_known_bin_dirs(
    *,
    platform: str = sys.platform,
    home: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> list[Path]:
    """Where package managers put executables, in the order they are searched.

    Only *candidates*: callers keep the ones that exist. The list is per
    platform because the same manager uses different roots (Homebrew on
    Apple silicon vs Intel, Linuxbrew) and Windows installers each pick
    their own directory.
    """
    home = home or Path.home()
    if platform == "win32":

        def env_path(name: str, *parts: str) -> list[Path]:
            root = environ.get(name)
            return [Path(root, *parts)] if root else []

        return [
            *env_path("ProgramFiles", "GitHub CLI"),
            *env_path("ProgramFiles(x86)", "GitHub CLI"),
            *env_path("LOCALAPPDATA", "Programs", "GitHub CLI"),
            *env_path("LOCALAPPDATA", "Microsoft", "WinGet", "Links"),
            home / "scoop" / "shims",
            *env_path("ProgramData", "chocolatey", "bin"),
            *env_path("ProgramFiles", "Git", "cmd"),
            *env_path("ProgramFiles", "Git", "bin"),
        ]
    if platform == "darwin":
        return [
            Path("/opt/homebrew/bin"),
            Path("/opt/homebrew/sbin"),
            Path("/usr/local/bin"),
            Path("/usr/local/sbin"),
            Path("/opt/local/bin"),  # MacPorts
            home / ".local" / "bin",  # pipx, uv tool
            home / "bin",
            home / ".nix-profile" / "bin",
            Path("/nix/var/nix/profiles/default/bin"),
        ]
    return [
        Path("/usr/local/bin"),
        Path("/usr/local/sbin"),
        home / ".local" / "bin",
        home / "bin",
        Path("/home/linuxbrew/.linuxbrew/bin"),
        home / ".linuxbrew" / "bin",
        Path("/snap/bin"),
        home / ".nix-profile" / "bin",
        Path("/nix/var/nix/profiles/default/bin"),
    ]


def _key(entry: str, platform: str) -> str:
    norm = os.path.normpath(entry)
    return norm.lower() if platform == "win32" else norm


def extend_path(
    environ: Mapping[str, str] = os.environ,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
) -> tuple[str, list[str]]:
    """``PATH`` with the existing well-known directories appended.

    Returns the new value and the directories it added. Pure apart from the
    ``is_dir`` probes, so the policy is testable with a fake environment.
    """
    current = [p for p in environ.get("PATH", "").split(os.pathsep) if p]
    seen = {_key(p, platform) for p in current}
    added: list[str] = []
    for candidate in well_known_bin_dirs(platform=platform, home=home, environ=environ):
        entry = str(candidate)
        key = _key(entry, platform)
        if key in seen or not candidate.is_dir():
            continue
        seen.add(key)
        added.append(entry)
    return os.pathsep.join([*current, *added]), added


def ensure_service_path(
    environ: MutableMapping[str, str] = os.environ,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
) -> list[str]:
    """Extend ``environ['PATH']`` in place; returns the directories added."""
    path, added = extend_path(environ, platform=platform, home=home)
    if added:
        environ["PATH"] = path
    return added


def extended_environ(
    environ: Mapping[str, str] = os.environ,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
) -> dict[str, str]:
    """A copy of ``environ`` for a subprocess, with the extended ``PATH``.

    A program found this way often spawns others (``gh`` runs ``git`` and
    credential helpers), so the children must see the same directories.
    """
    env = dict(environ)
    env["PATH"], _ = extend_path(environ, platform=platform, home=home)
    return env


def resolve_executable(
    name: str,
    environ: Mapping[str, str] = os.environ,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
) -> str | None:
    """The path of ``name`` on the extended ``PATH``, or None."""
    path, _ = extend_path(environ, platform=platform, home=home)
    return shutil.which(name, path=path)


def missing_executable_message(
    name: str,
    environ: Mapping[str, str] = os.environ,
    *,
    platform: str = sys.platform,
    home: Path | None = None,
) -> str:
    """Why ``name`` could not be run, with the directories that were searched."""
    path, _ = extend_path(environ, platform=platform, home=home)
    searched = ", ".join(p for p in path.split(os.pathsep) if p) or "(empty PATH)"
    hint = {
        "gh": "Install GitHub CLI (https://cli.github.com — e.g. `brew install gh`, `winget install GitHub.cli`) "
        "and log in with `gh auth login`.",
        "git": "Install git (https://git-scm.com) — on macOS `xcode-select --install` is enough.",
    }.get(name, f"Install `{name}` into one of those directories.")
    return (
        f"`{name}` is not installed, or not in a directory the engine searches. "
        f"The engine runs as a background service and does not read your shell profile, so a "
        f"directory that works in your terminal may be missing here. Searched: {searched}. {hint}"
    )
