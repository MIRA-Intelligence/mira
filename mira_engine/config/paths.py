"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from mira_engine.config.loader import get_config_path, get_home_dir
from mira_engine.utils.helpers import ensure_dir

RUNTIME_DIR_ENV = "MIRA_RUNTIME_DIR"


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def runtime_home_key(home: Path) -> str:
    """Return a short stable key that namespaces one MIRA_HOME in a shared runtime dir."""
    resolved = Path(home).expanduser().resolve(strict=False)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]


def _is_private_dir(path: Path) -> bool:
    """Whether ``path`` is a directory owned by this user and closed to everyone else."""
    if os.name == "nt":
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _private_subdir(base: Path, home: Path) -> Path:
    path = base / runtime_home_key(home)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def get_runtime_dir(home: Path | None = None) -> Path:
    """Return the local-disk directory for locks, leases and other ephemeral state.

    Data stays in MIRA_HOME, which may live on NFS where locks are unreliable.
    Resolution: ``MIRA_RUNTIME_DIR`` > ``$XDG_RUNTIME_DIR/mira`` (only when that
    dir is private to this user) > ``<MIRA_HOME>/runtime``. Shared bases get one
    subdirectory per MIRA_HOME so two homes never collide.
    """
    home = Path(home).expanduser() if home is not None else get_home_dir()
    override = os.environ.get(RUNTIME_DIR_ENV, "").strip()
    if override:
        return _private_subdir(Path(override).expanduser(), home)
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg and _is_private_dir(Path(xdg)):
        try:
            return _private_subdir(Path(xdg) / "mira", home)
        except OSError:
            pass
    return ensure_dir(home / "runtime")


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else get_home_dir() / "workspace"
    return ensure_dir(path)


def is_default_workspace(workspace: str | Path | None) -> bool:
    """Return whether workspace resolves to mira's default workspace path."""
    current = (
        Path(workspace).expanduser()
        if workspace is not None
        else get_home_dir() / "workspace"
    )
    default = get_home_dir() / "workspace"
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return get_home_dir() / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return get_home_dir() / "bridge"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return get_home_dir() / "sessions"


def get_knowledge_dir() -> Path:
    """Return the root of the user's knowledge bases."""
    return get_runtime_subdir("knowledge")
