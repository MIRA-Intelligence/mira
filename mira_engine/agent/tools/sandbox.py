"""bubblewrap sandbox for agent shell commands (``tools.exec.sandbox = "bwrap"``).

The sandbox starts from an empty root and mounts only what a command needs:

- system directories read-only (``/usr``, ``/etc``, ``/opt``, ...), ``/proc``
  (own pid namespace), a minimal ``/dev`` plus GPU device nodes, ``/sys``
  read-only and a private ``/tmp``;
- ``$HOME`` backed by ``<MIRA_HOME>/sandbox/home``, so caches and ``pip
  --user`` installs persist between commands but the real home stays hidden;
- the uv cache and uv-managed interpreters, ``~/.local/bin``, the engine's own
  install, skill directories, the media directory and admin or user extra
  paths;
- the Mira home itself (config, provider keys, the gateway token) masked by an
  empty tmpfs;
- the project workspace read-write.

The whole shell runs inside (``bwrap ... -- bash -c <command>``), with the
curated environment from :class:`ExecTool`. ``network="none"`` adds a private
network namespace. If bwrap is missing or cannot create namespaces (for
example Ubuntu's AppArmor restriction on unprivileged user namespaces), the
command is refused rather than run unsandboxed.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from mira_engine.config.paths import get_media_dir

BWRAP = "bwrap"
_SYSTEM_READ_ONLY = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt",
                     "/nix", "/run/systemd/resolve")
_GPU_DEVICES = ("/dev/nvidia*", "/dev/dri")
_PROBE_RETRY_S = 60.0


def _mira_home() -> Path:
    from mira_engine.config.loader import get_home_dir

    return get_home_dir().expanduser().resolve()


def _resolve(path: str | Path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def private_dirs() -> tuple[Path, ...]:
    """Private data a sandboxed command must never see; the host-side file tools read it.

    A sandbox may reach the network, so anything it can read can leave the
    machine (``curl -F @...``). The long-term mind (USER.md, MEMORY.md, the
    history) therefore stays out of every sandbox.
    """
    from mira_engine.config.paths import get_mind_dir

    return (get_mind_dir().resolve(),)


def _exposes(path: Path, private: tuple[Path, ...]) -> bool:
    """Whether mounting ``path`` would show any of ``private``."""
    return any(_inside(path, p) or _inside(p, path) for p in private)


def _uv_dirs(home: Path) -> list[Path]:
    """The uv cache and interpreter dirs, shared with venvs created outside the sandbox."""
    cache = os.environ.get("UV_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or str(home / ".cache"), "uv")
    python = os.environ.get("UV_PYTHON_INSTALL_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share"), "uv", "python")
    return [Path(cache), Path(python)]


@dataclass(frozen=True)
class SandboxSpec:
    """What one sandboxed command may see."""

    workspace: Path
    network: bool = True
    read_only: tuple[Path, ...] = ()
    read_write: tuple[Path, ...] = ()
    extra_read_only: tuple[Path, ...] = field(default=())  # e.g. skills, the uv binary

    @classmethod
    def build(
        cls,
        workspace: str | Path,
        *,
        network: str = "full",
        read_only_paths: list[str] | tuple[str, ...] = (),
        read_write_paths: list[str] | tuple[str, ...] = (),
        extra_read_only: list[Path] | tuple[Path, ...] = (),
    ) -> SandboxSpec:
        def paths(values: list[str] | tuple[str, ...] | list[Path] | tuple[Path, ...]) -> tuple[Path, ...]:
            resolved = (_resolve(value) for value in values if str(value).strip())
            return tuple(dict.fromkeys(path for path in resolved if path is not None))

        private = private_dirs()
        read_only, read_write = paths(read_only_paths), paths(read_write_paths)
        # Admin and user paths are mounted before the Mira home is masked, so
        # only one inside private data could show it: refuse rather than hide it.
        for path in (*read_only, *read_write):
            if any(_inside(path, p) for p in private):
                raise ValueError(f"{path} is private (the long-term mind) and cannot be mounted in the sandbox")
        return cls(
            workspace=Path(workspace).expanduser().resolve(),
            network=network != "none",
            read_only=read_only,
            read_write=read_write,
            # The engine passes the mind with the skill dirs for its own file tools.
            extra_read_only=tuple(p for p in paths(extra_read_only) if not _exposes(p, private)),
        )


def _sandbox_home(mira_home: Path) -> Path:
    path = mira_home / "sandbox" / "home"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def bwrap_argv(spec: SandboxSpec, cwd: str | Path | None = None) -> list[str]:
    """``bwrap`` arguments up to and including ``--``; append the command argv."""
    home = Path(os.environ.get("HOME") or Path.home())
    mira_home = _mira_home()
    workspace = spec.workspace
    target_cwd = _resolve(cwd) if cwd is not None else workspace
    if target_cwd is None or not _inside(target_cwd, workspace):
        target_cwd = workspace

    args = [BWRAP, "--new-session", "--die-with-parent", "--unshare-pid", "--unshare-ipc",
            "--unshare-uts", "--unshare-cgroup-try"]
    if not spec.network:
        args.append("--unshare-net")
    for path in _SYSTEM_READ_ONLY:
        args += ["--ro-bind-try", path, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--ro-bind-try", "/sys", "/sys"]
    for pattern in _GPU_DEVICES:
        for device in sorted(glob.glob(pattern)):
            args += ["--dev-bind-try", device, device]
    args += ["--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
    args += ["--bind", str(_sandbox_home(mira_home)), str(home)]
    for path in spec.read_only:
        args += ["--ro-bind-try", str(path), str(path)]
    for path in spec.read_write:
        args += ["--bind-try", str(path), str(path)]
    shared = [home / ".local" / "bin", Path(sys.prefix), Path(sys.base_prefix)]
    media = _resolve(get_media_dir())
    private = private_dirs()
    read_only = [path for path in [*shared, *([media] if media else []), *spec.extra_read_only]
                 if path.exists() and not _exposes(path, private)]

    def mask_mira_home(only_inside: bool) -> list[str]:
        # Nothing before this may expose the Mira home (config.json, keys, the
        # gateway token); then the parts commands need are mounted again.
        masked = ["--tmpfs", str(mira_home)]
        for path in read_only:
            if not only_inside or _inside(path, mira_home):
                masked += ["--ro-bind", str(path), str(path)]
        return masked

    args += mask_mira_home(only_inside=False)
    for path in _uv_dirs(home):
        if path.is_dir():
            args += ["--bind", str(path), str(path)]
    args += ["--bind", str(workspace), str(workspace)]
    if _inside(mira_home, workspace):
        # A workspace that contains the Mira home (e.g. the home directory).
        args += mask_mira_home(only_inside=True)
    args += ["--chdir", str(target_cwd), "--"]
    return args


# ── availability ─────────────────────────────────────────────────────

_probe_lock = threading.Lock()
_probe_cache: dict[bool, tuple[bool, str, float]] = {}


def _probe(network: bool) -> tuple[bool, str]:
    if not sys.platform.startswith("linux"):
        return False, "the bwrap sandbox is only available on Linux"
    binary = shutil.which(BWRAP)
    if binary is None:
        return False, "bwrap (bubblewrap) is not installed"
    with tempfile.TemporaryDirectory(prefix="mira-sandbox-probe-") as tmp:
        argv = bwrap_argv(SandboxSpec(workspace=Path(tmp).resolve(), network=network)) + ["/bin/sh", "-c", "true"]
        argv[0] = binary
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"bwrap could not run: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, "bwrap cannot create a sandbox here" + (f": {detail[-1]}" if detail else "")
    return True, ""


def sandbox_available(network: bool = True) -> tuple[bool, str]:
    """Whether bwrap works on this host; failures are re-checked after a minute."""
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(network)
        if cached is not None and (cached[0] or now - cached[2] < _PROBE_RETRY_S):
            return cached[0], cached[1]
    ok, reason = _probe(network)
    with _probe_lock:
        _probe_cache[network] = (ok, reason, now)
    return ok, reason


def reset_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def sandbox_status(mode: str, *, network: str = "full") -> dict[str, object]:
    """Sandbox state for /api/status."""
    if mode != "bwrap":
        return {"mode": mode or None, "network": None, "available": None, "reason": None}
    ok, reason = sandbox_available(network != "none")
    return {"mode": mode, "network": network, "available": ok, "reason": reason or None}
