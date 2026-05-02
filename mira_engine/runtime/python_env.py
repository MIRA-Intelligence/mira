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


def _bundled_uv_candidates() -> list[Path]:
    """Probe well-known locations where a PyInstaller bundle stashes ``uv``.

    Returns an ordered list of ``Path`` objects, each of which **may or may
    not exist**. Callers should test each with :meth:`Path.is_file` before
    invoking it. The order is deliberate: the one-file ``sys._MEIPASS``
    extraction directory is tried before any sibling-of-executable path
    because the former is the canonical location written by PyInstaller's
    ``binaries=[(uv, '.')]`` directive.
    """
    candidates: list[Path] = []
    name = "uv.exe" if sys.platform == "win32" else "uv"

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / name)

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / name)
        candidates.append(exe_dir / "_internal" / name)

    return candidates


def detect_uv(*, search_path: str | None = None) -> UvBinary | None:
    """Return the first usable ``uv``.

    Search order:
    1. Any candidate stashed inside the running PyInstaller bundle
       (``sys._MEIPASS`` or alongside ``sys.executable``). This makes
       ``uv`` available out of the box for users of the bundled
       ``mira-engine`` desktop release.
    2. ``shutil.which("uv", path=search_path)`` — PATH-based discovery
       for source / pip installs.

    A candidate is considered "usable" if ``uv --version`` exits 0 and
    reports a version >= :data:`MIN_UV_VERSION`. Older binaries are
    rejected — the caller should surface a friendly upgrade hint rather
    than silently falling back.
    """
    candidates: list[str] = []
    for candidate in _bundled_uv_candidates():
        if candidate.is_file():
            candidates.append(str(candidate))

    path_hit = shutil.which("uv", path=search_path)
    if path_hit:
        candidates.append(path_hit)

    for binary in candidates:
        result = _query_uv_version(binary)
        if result is not None:
            return result
    return None


def _query_uv_version(binary: str) -> UvBinary | None:
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

    if cfg.python_version:
        ensure_python_interpreter(binary, cfg.python_version, env=env)

    _create_venv(binary, venv, cfg, env=env, cwd=project)
    _install_initial_dependencies(binary, project, venv, cfg, env=env)

    return venv


def ensure_python_interpreter(
    uv: UvBinary,
    version: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Make sure ``uv`` has the requested CPython available locally.

    On first launch the bundled ``uv`` has zero pre-installed
    interpreters; ``uv venv --python 3.11`` would either auto-download
    silently (recent uv) or fail (older uv). This helper makes the
    download explicit so:

    * the user sees a single up-front progress message ("installing
      Python 3.11...") rather than during every project venv creation;
    * we can fail fast with a useful error before bothering with venv
      creation;
    * desktop launchers (PyInstaller bundle) can invoke it once at
      first launch via ``mira runtime install-python`` so the rest of
      the session uses a warm cache.

    The function is idempotent: it consults ``uv python list
    --only-installed`` and short-circuits when the requested version
    is already present.

    Parameters
    ----------
    uv:
        Located ``uv`` binary (typically from :func:`detect_uv`).
    version:
        Either a major.minor (``"3.11"``) or a full version
        (``"3.11.10"``) accepted by ``uv python install``.
    env:
        Optional process environment for the subprocess. When ``None``,
        ``os.environ`` is inherited.
    """
    if _interpreter_installed(uv, version, env=env):
        logger.debug("uv: python %s already installed", version)
        return
    logger.info("uv: installing python %s (one-time)", version)
    _run(
        [str(uv.path), "python", "install", version],
        env=env if env is not None else os.environ.copy(),
        cwd=Path.cwd(),
        action=f"install python {version}",
    )


def _interpreter_installed(
    uv: UvBinary,
    version: str,
    *,
    env: dict[str, str] | None,
) -> bool:
    """True iff ``uv python list --only-installed`` mentions ``version``.

    The output format is one entry per line, e.g.::

        cpython-3.11.10-macos-aarch64-none    /path/to/uv/python/...

    We do a substring match on the major.minor (or full) version so the
    check works for both ``"3.11"`` and ``"3.11.10"`` callers.
    """
    try:
        result = subprocess.run(
            [str(uv.path), "python", "list", "--only-installed"],
            env=env if env is not None else os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("uv python list failed: %s", exc)
        return False
    if result.returncode != 0:
        return False
    needle = f"-{version}" if version.count(".") >= 1 else version
    for line in (result.stdout or "").splitlines():
        if needle in line:
            return True
    return False


# ---------------------------------------------------------------------------
# Cache & venv housekeeping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenvInfo:
    """Discovered project venv on disk."""

    venv_path: Path
    project_dir: Path
    size_bytes: int
    last_used: float  # epoch seconds; the most-recent mtime under the venv
    last_project_activity: float  # most-recent mtime of project files (excl. venv)


def find_project_venvs(
    root: Path | str,
    *,
    venv_dir_name: str = ".venv",
    max_depth: int = 6,
) -> list[VenvInfo]:
    """Walk ``root`` and return every directory whose basename matches
    ``venv_dir_name`` and which looks like a venv (has ``pyvenv.cfg``).

    The walk is bounded at ``max_depth`` to avoid runaway scans on huge
    workspaces. Symlinks are not followed.

    For each hit we collect:

    - on-disk size (sum of file sizes, hardlinks counted once);
    - ``last_used`` — newest mtime of any file under the venv (rough
      proxy for "the agent ran something via this interpreter
      recently");
    - ``last_project_activity`` — newest mtime of project files
      *outside* the venv. Stale = project untouched for a while.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return []

    found: list[VenvInfo] = []
    for venv in _walk_for_venvs(root, venv_dir_name, max_depth):
        project = venv.parent
        size = _venv_size_bytes(venv)
        last_used = _newest_mtime(venv)
        last_activity = _newest_mtime_excluding(project, venv)
        found.append(
            VenvInfo(
                venv_path=venv,
                project_dir=project,
                size_bytes=size,
                last_used=last_used,
                last_project_activity=last_activity,
            )
        )
    return sorted(found, key=lambda v: v.size_bytes, reverse=True)


def _walk_for_venvs(root: Path, name: str, max_depth: int):
    """Yield candidate venv directories without descending into any."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name == name and (entry / "pyvenv.cfg").is_file():
                        yield entry
                        # don't descend into a venv
                        continue
                    stack.append((entry, depth + 1))
            except OSError:
                continue


def _venv_size_bytes(venv: Path) -> int:
    """Sum file sizes under ``venv``, counting each inode once."""
    total = 0
    seen: set[tuple[int, int]] = set()
    for path in venv.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)
        total += stat.st_size
    return total


def _newest_mtime(path: Path) -> float:
    newest = 0.0
    for child in path.rglob("*"):
        try:
            mt = child.stat().st_mtime
        except OSError:
            continue
        if mt > newest:
            newest = mt
    return newest


def _newest_mtime_excluding(root: Path, exclude: Path) -> float:
    newest = 0.0
    try:
        children = list(root.iterdir())
    except OSError:
        return newest
    for child in children:
        try:
            if child == exclude:
                continue
            if child.is_dir():
                inner = _newest_mtime(child)
                if inner > newest:
                    newest = inner
            else:
                mt = child.stat().st_mtime
                if mt > newest:
                    newest = mt
        except OSError:
            continue
    return newest


def prune_uv_cache(
    uv: UvBinary | None = None,
    *,
    cache_dir: str | None = None,
    dry_run: bool = False,
) -> str:
    """Run ``uv cache prune`` and return its stdout.

    ``uv cache prune`` removes packages from the global content-addressed
    cache that are no longer referenced by any ``uv.lock`` or
    pre-existing venv. Hardlinks mean removed packages are usually
    already disk-free if some venv still pins them.

    Raises :class:`PythonEnvError` on failure.
    """
    binary = uv or detect_uv()
    if binary is None:
        raise PythonEnvError("uv is required for cache prune but was not found.")
    args: list[str] = [str(binary.path), "cache", "prune"]
    if dry_run:
        args.append("--dry-run")
    env = os.environ.copy()
    if cache_dir:
        env["UV_CACHE_DIR"] = str(Path(cache_dir).expanduser())
    try:
        result = subprocess.run(
            args,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PythonEnvError(f"failed to prune uv cache: {exc}") from exc
    if result.returncode != 0:
        raise PythonEnvError(
            f"uv cache prune failed (exit={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return (result.stdout or result.stderr or "").strip()


def remove_venv(venv: Path) -> int:
    """Recursively delete a venv directory. Returns reclaimed bytes.

    Hardlink-aware: a deleted file that's also linked under the uv
    cache won't actually free disk space, but the byte count returned
    here reflects the venv's *apparent* size (sum of file sizes), which
    is the user-facing number we want to report.
    """
    if not venv.exists():
        return 0
    size = _venv_size_bytes(venv)
    import shutil as _shutil
    _shutil.rmtree(venv, ignore_errors=False)
    return size


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
