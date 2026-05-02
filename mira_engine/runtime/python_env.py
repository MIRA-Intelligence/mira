"""Per-project Python environment management via ``uv``.

This module is **side-effect free at import time**. Nothing here is wired
into the exec tool yet; PR 4 in the milestone (`Per-project Python
environments`) will start consuming :func:`ensure_project_venv` from
``ExecTool``. Until then this layer is exercised exclusively by unit tests.

Design notes
------------

1. **uv is the only supported manager** today. ``manager == "system"`` is
   defined in the schema but not implemented here; calling
   :func:`ensure_project_venv` for a non-``uv`` manager is a no-op that
   returns ``None``.

2. **Idempotent** — every helper checks for the desired end state before
   shelling out. Calling :func:`ensure_project_venv` twice on the same
   project does at most one ``uv venv`` and one dependency sync.

3. **Synchronous subprocess** calls. The exec tool itself is async, but
   bootstrapping a venv blocks the agent for at most a few seconds
   (subsequent calls are sub-millisecond) and async-wrapping every
   shell-out would force every test to use ``pytest-asyncio``.

4. **No global state**: every helper takes the project directory and
   config explicitly. This makes the module trivially testable and lets
   future code reuse it for non-exec contexts (e.g. workspace bootstrap).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mira_engine.config.schema import PythonRuntimeConfig

logger = logging.getLogger(__name__)

# Minimum supported uv. 0.5.0 ships ``uv python install`` and stable
# hardlink semantics; earlier versions miss either or both.
MIN_UV_VERSION: tuple[int, int, int] = (0, 5, 0)

_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)")

# Commands that imply the agent expects a Python interpreter / package
# manager on PATH. Used by callers (PR 4) to decide whether to bootstrap
# a venv before spawning the subprocess. Matched as a leading token after
# trimming pipes / sequencing operators.
PYTHON_COMMAND_TOKENS: tuple[str, ...] = (
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "ipython",
    "jupyter",
    "uv",
)


@dataclass(frozen=True)
class UvBinary:
    """A located ``uv`` executable and its detected version."""

    path: Path
    version: tuple[int, int, int]

    def is_at_least(self, target: tuple[int, int, int]) -> bool:
        return self.version >= target


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_uv(*, search_path: str | None = None) -> UvBinary | None:
    """Return the first usable ``uv`` on ``$PATH`` (or ``search_path``).

    A binary is considered "usable" if ``uv --version`` exits 0 and
    reports a version >= :data:`MIN_UV_VERSION`. Older binaries are
    rejected — caller should surface a friendly upgrade hint rather than
    silently falling back.
    """
    binary = shutil.which("uv", path=search_path)
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("uv --version failed at %s: %s", binary, exc)
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(result.stdout or result.stderr)
    if not match:
        return None
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if version < MIN_UV_VERSION:
        logger.warning(
            "Found uv %s at %s but require >= %s; treating as missing.",
            ".".join(map(str, version)),
            binary,
            ".".join(map(str, MIN_UV_VERSION)),
        )
        return None
    return UvBinary(path=Path(binary), version=version)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def project_venv_path(project_dir: Path | str, cfg: PythonRuntimeConfig) -> Path:
    """Resolve the absolute path of a project's venv directory."""
    venv = Path(cfg.venv_dir)
    return venv if venv.is_absolute() else (Path(project_dir) / venv)


def venv_bin_dir(venv: Path) -> Path:
    """Return ``Scripts/`` on Windows, ``bin/`` elsewhere."""
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


def venv_python_path(venv: Path) -> Path:
    """Resolve the python interpreter inside a venv."""
    name = "python.exe" if sys.platform == "win32" else "python"
    return venv_bin_dir(venv) / name


def venv_exists(venv: Path) -> bool:
    """Idempotency check — true if the venv directory is plausibly complete."""
    return venv.exists() and venv_python_path(venv).exists()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def ensure_project_venv(
    project_dir: Path | str,
    cfg: PythonRuntimeConfig,
    *,
    uv: UvBinary | None = None,
    extra_env: dict[str, str] | None = None,
) -> Path | None:
    """Make sure ``<project_dir>/<venv_dir>`` is a usable venv.

    Returns the absolute venv path on success, ``None`` when the manager
    is disabled. Raises :class:`PythonEnvError` on hard failures (no uv,
    uv command failed, etc.) so callers can choose to fall back to legacy
    behaviour.

    The function is **idempotent**: if the venv already exists, no
    subprocess is spawned and dependencies are not re-synced (a separate
    explicit ``mira project sync`` will be added later for that).
    """
    if cfg.manager != "uv":
        return None

    project = Path(project_dir).resolve()
    venv = project_venv_path(project, cfg)

    if venv_exists(venv):
        return venv

    binary = uv or detect_uv()
    if binary is None:
        raise PythonEnvError(
            "uv is required for tools.exec.python.manager='uv' but was not found "
            "on PATH (or is older than %s). Install from "
            "https://docs.astral.sh/uv/ or set the cli-config manager back to 'off'."
            % ".".join(map(str, MIN_UV_VERSION))
        )

    env = _build_uv_env(cfg, extra_env)

    _create_venv(binary, venv, cfg, env=env, cwd=project)
    _install_initial_dependencies(binary, project, venv, cfg, env=env)

    return venv


# ---------------------------------------------------------------------------
# Internal subprocess helpers
# ---------------------------------------------------------------------------


def _build_uv_env(
    cfg: PythonRuntimeConfig, extra: dict[str, str] | None
) -> dict[str, str]:
    """Compose the env for ``uv`` subprocess calls."""
    env = os.environ.copy()
    if cfg.cache_dir:
        env["UV_CACHE_DIR"] = str(Path(cfg.cache_dir).expanduser())
    if cfg.link_mode:
        env["UV_LINK_MODE"] = cfg.link_mode
    # Don't let uv pick up an outer venv during bootstrap; we want it to
    # build a fresh one targeted at the project directory.
    env.pop("VIRTUAL_ENV", None)
    if extra:
        env.update(extra)
    return env


def _create_venv(
    uv: UvBinary,
    venv: Path,
    cfg: PythonRuntimeConfig,
    *,
    env: dict[str, str],
    cwd: Path,
) -> None:
    args: list[str] = [str(uv.path), "venv", str(venv)]
    if cfg.python_version:
        args += ["--python", cfg.python_version]
    if cfg.link_mode:
        args += ["--link-mode", cfg.link_mode]
    _run(args, env=env, cwd=cwd, action=f"create venv at {venv}")


def _install_initial_dependencies(
    uv: UvBinary,
    project: Path,
    venv: Path,
    cfg: PythonRuntimeConfig,
    *,
    env: dict[str, str],
) -> None:
    """Install initial deps: prefer pyproject.toml/uv.lock → requirements.txt → baseline.

    The strategy mirrors what a developer would type by hand: if the
    project already declares its deps somewhere, we honour that; otherwise
    we fall back to the configurable baseline.
    """
    pyproject = project / "pyproject.toml"
    requirements = project / "requirements.txt"
    install_env = dict(env)
    install_env["VIRTUAL_ENV"] = str(venv)

    if pyproject.exists():
        _run(
            [str(uv.path), "sync"],
            env=install_env,
            cwd=project,
            action="uv sync project dependencies",
        )
        return

    if requirements.exists():
        _run(
            [str(uv.path), "pip", "install", "-r", str(requirements)],
            env=install_env,
            cwd=project,
            action="install requirements.txt",
        )
        return

    if cfg.baseline_requirements:
        _run(
            [str(uv.path), "pip", "install", *cfg.baseline_requirements],
            env=install_env,
            cwd=project,
            action="install baseline requirements",
        )


def _run(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    action: str,
) -> None:
    logger.info("uv: %s — %s", action, " ".join(args))
    try:
        result = subprocess.run(
            args,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PythonEnvError(f"failed to {action}: {exc}") from exc
    if result.returncode != 0:
        raise PythonEnvError(
            f"failed to {action} (exit={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )


class PythonEnvError(RuntimeError):
    """Raised when the project venv cannot be created or synced."""
