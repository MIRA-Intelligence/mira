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

        return cls(
            workspace=Path(workspace).expanduser().resolve(),
            network=network != "none",
            read_only=paths(read_only_paths),
            read_write=paths(read_write_paths),
            extra_read_only=paths(extra_read_only),
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
    read_only = [path for path in [*shared, *([media] if media else []), *spec.extra_read_only] if path.exists()]

    # Granted paths strictly inside the Mira home (a plugin under
    # <home>/plugins, the default workspace <home>/workspace) would be hidden
    # by the mask below, so they are mounted again after it; the home itself
    # and its ancestors never are.
    granted_inside = [
        *(("--ro-bind-try", path) for path in spec.read_only if path != mira_home and _inside(path, mira_home)),
        *(("--bind-try", path) for path in spec.read_write if path != mira_home and _inside(path, mira_home)),
    ]

    def mask_mira_home(only_inside: bool) -> list[str]:
        # Nothing before this may expose the Mira home (config.json, keys, the
        # gateway token); then the parts commands need are mounted again.
        masked = ["--tmpfs", str(mira_home)]
        for path in read_only:
            if not only_inside or _inside(path, mira_home):
                masked += ["--ro-bind", str(path), str(path)]
        for flag, path in granted_inside:
            masked += [flag, str(path), str(path)]
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


# ── macOS: Seatbelt (sandbox-exec) for out-of-process plugins ─────────

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_seatbelt_cache: tuple[bool, str] | None = None


def _sb(path: Path | str) -> str:
    """A path as an SBPL string literal."""
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _seatbelt_read_roots(hidden: list[Path]) -> list[Path]:
    """What a Python host needs to read: its interpreter and its import path
    — never a directory that contains the home or the Mira home (``sys.path``
    can hold the current directory, which may be the home itself)."""
    roots = [Path(sys.prefix), Path(sys.base_prefix), Path(sys.exec_prefix), Path(sys.executable).parent]
    roots += [Path(p) for p in sys.path if p]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    out: list[Path] = []
    for root in roots:
        resolved = _resolve(root)
        if resolved is None or not resolved.exists() or resolved in out:
            continue
        if any(_inside(h, resolved) for h in hidden):
            continue
        out.append(resolved)
    return out


def seatbelt_profile(spec: SandboxSpec, *, shell: bool = True, hide: list[Path] | tuple[Path, ...] = ()) -> str:
    """An SBPL profile for one plugin host (later rules win, so each deny is
    followed by what it must still allow):

    - no network unless the spec allows it (AF_UNIX stays, for asyncio);
    - writes only to the scratch dir (``spec.workspace``), ``read_write``
      grants and ``/dev``;
    - no reading the contents of anything in the home directory, the Mira
      home or ``hide`` (e.g. a workspace the plugin wasn't granted) except
      the interpreter and its import path, ``read_only`` / ``extra_read_only``
      grants and the scratch dir;
    - without ``shell``, no executing anything but the interpreter itself.
    """
    home = _resolve(os.environ.get("HOME") or Path.home())
    mira_home = _mira_home()
    lines = ["(version 1)", "(allow default)"]
    if not spec.network:
        lines.append("(deny network-outbound (remote ip))")
        lines.append("(deny network-bind (local ip))")
    lines.append('(deny file-write* (subpath "/"))')
    writable = [spec.workspace, *spec.read_write]
    lines.append("(allow file-write* " + " ".join(f"(subpath {_sb(p)})" for p in writable) + ' (subpath "/dev"))')
    hidden = [p for p in (home, mira_home, *(_resolve(h) for h in hide)) if p is not None]
    lines.append("(deny file-read-data " + " ".join(f"(subpath {_sb(p)})" for p in hidden) + ")")
    readable = [*_seatbelt_read_roots(hidden), *spec.read_only, *spec.extra_read_only, *writable]
    lines.append("(allow file-read-data " + " ".join(f"(subpath {_sb(p)})" for p in readable) + ")")
    if not shell:
        lines.append("(deny process-exec*)")
        lines.append("(allow process-exec* " + " ".join(f"(literal {_sb(p)})" for p in _interpreters()) + ")")
    return "\n".join(lines)


def _interpreters() -> list[Path]:
    """The executables starting the host may run: the interpreter (or the
    frozen app), and on a macOS framework build the ``Python.app`` binary it
    re-executes itself as."""
    candidates = [Path(sys.executable), Path(getattr(sys, "_base_executable", "") or sys.executable)]
    candidates.append(Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python")
    out: list[Path] = []
    for candidate in candidates:
        for path in (candidate, _resolve(candidate)):
            if path is not None and path.exists() and path not in out:
                out.append(path)
    return out


def seatbelt_argv(spec: SandboxSpec, *, shell: bool = True, hide: list[Path] | tuple[Path, ...] = ()) -> list[str]:
    """``sandbox-exec`` arguments; append the command argv."""
    return [SANDBOX_EXEC, "-p", seatbelt_profile(spec, shell=shell, hide=hide)]


def seatbelt_available() -> tuple[bool, str]:
    """Whether sandbox-exec works here (macOS)."""
    global _seatbelt_cache
    if _seatbelt_cache is not None:
        return _seatbelt_cache
    if sys.platform != "darwin":
        _seatbelt_cache = (False, "the Seatbelt sandbox is only available on macOS")
    elif not Path(SANDBOX_EXEC).exists():
        _seatbelt_cache = (False, "sandbox-exec is not available")
    else:
        try:
            result = subprocess.run(
                [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            ok = result.returncode == 0
            _seatbelt_cache = (ok, "" if ok else f"sandbox-exec failed: {(result.stderr or '').strip()[-200:]}")
        except (OSError, subprocess.SubprocessError) as exc:
            _seatbelt_cache = (False, f"sandbox-exec could not run: {exc}")
    return _seatbelt_cache


def plugin_sandbox(network: bool = True) -> tuple[str | None, str]:
    """The sandbox for an out-of-process plugin here: ``"bwrap"`` (Linux),
    ``"seatbelt"`` (macOS) or None with the reason (e.g. Windows)."""
    if sys.platform == "darwin":
        ok, reason = seatbelt_available()
        return ("seatbelt" if ok else None), reason
    ok, reason = sandbox_available(network=network)
    return ("bwrap" if ok else None), reason
