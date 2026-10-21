"""In-process permission tripwire for plugins (design §3.5-3.6).

A plugin running inside the engine can't be sandboxed: Python lets it reset a
context variable, use ctypes or start a thread the engine never sees. So this
is a **tripwire, not a sandbox** — it catches a community plugin doing what its
manifest didn't declare, through the ordinary APIs, and stops it there. The
sandbox is for plugins the platform hasn't vouched for, which run out of
process (:mod:`.isolation`).

While a plugin's code runs — a tool call, a hook, a channel (started, sending,
stopping), the plugin's import and construction — a :class:`Grant` for it is
set in a context variable. One ``sys.addaudithook`` checks these events
against it:

* **network** — ``socket.connect`` to an internet address when the manifest
  declares no network. (Host names can't be checked in-process: asyncio
  resolves them in executor threads, outside the grant; the names are logged.)
* **files** — writes (``open`` for writing, ``os.remove/rename/mkdir/rmdir``,
  ``shutil.rmtree``, ``sqlite3.connect``) outside the plugin's data dir, the
  temp dir, the media dir and — with ``workspace:write`` — the workspace;
  and reads inside the Mira home (config, keys, the gateway token) other than
  those places and the plugin itself.
* **shell** — starting processes when ``shell`` isn't declared.

``community`` and ``verified`` plugins get a ``PermissionError`` (their tool
call fails with it). ``core-bundled`` plugins aren't checked unless
``MIRA_PLUGIN_AUDIT=1`` is set, and then violations are only logged.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

_WATCHED = frozenset({
    "open", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "shutil.rmtree", "sqlite3.connect",
    "socket.connect", "socket.getaddrinfo",
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


@dataclass(frozen=True)
class Grant:
    plugin_id: str
    enforce: bool  # False: log only (core-bundled audit)
    network: bool
    hosts: tuple[str, ...] = ()
    shell: bool = False
    write_roots: tuple[str, ...] = ()  # explicit grants (data dir, media, workspace, ...)
    read_roots: tuple[str, ...] = ()  # explicit grants inside the Mira home
    temp_roots: tuple[str, ...] = ()  # writable, except where they overlap the Mira home
    mira_home: str = ""
    _reported: set[tuple[str, str]] = field(default_factory=set, compare=False, hash=False, repr=False)


_current: ContextVar[Grant | None] = ContextVar("mira_plugin_grant", default=None)
_installed = False
_install_lock = threading.Lock()
_inside = threading.local()


def current() -> Grant | None:
    return _current.get()


@contextmanager
def granted(grant: Grant | None) -> Iterator[None]:
    """Run the block as the plugin ``grant`` belongs to (no-op for None)."""
    if grant is None:
        yield
        return
    install()
    token = _current.set(grant)
    try:
        yield
    finally:
        _current.reset(token)


def _real(path: Any) -> str | None:
    if isinstance(path, int) or path is None:
        return None
    try:
        text = os.fsdecode(path)
    except TypeError:
        return None
    if not text:
        return None
    try:
        return os.path.realpath(os.path.abspath(text))
    except (OSError, ValueError):
        return None


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots if r)


def _temp_roots() -> list[str]:
    roots = {tempfile.gettempdir(), "/tmp", "/var/tmp"}
    return [r for r in (_real(p) for p in roots) if r]


def grant_for(
    manifest: Any,
    tier: str | None,
    *,
    workspace: Path | None = None,
    audit_bundled: bool = False,
    data_dir: Path | None = None,
    mira_home: Path | None = None,
) -> Grant | None:
    """The grant a plugin's in-process code runs under, or None when it isn't
    checked (core-bundled without ``MIRA_PLUGIN_AUDIT``)."""
    if tier == "core-bundled" and not audit_bundled:
        return None
    from mira_engine.config.paths import get_data_dir, get_media_dir

    perms = manifest.permissions
    # An out-of-process host is told these by the engine (its own environment
    # doesn't know the engine's Mira home).
    data_root = mira_home or get_data_dir()
    media = (mira_home / "media") if mira_home else get_media_dir()
    plugin_data = data_dir or data_root / "plugin-data" / manifest.id
    # Made here, by the engine: the plugin may write inside it, not create
    # its parents in the Mira home.
    if not plugin_data.is_dir():
        try:
            plugin_data.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass  # e.g. inside a sandbox that already made it, or hides its parent
    writes = [str(plugin_data), str(media), str(Path(manifest.root) / "__pycache__")]
    reads = [str(manifest.root), str(plugin_data), str(media)]
    if workspace is not None and perms.filesystem in ("workspace:read", "workspace:write"):
        reads.append(str(workspace))
        if perms.filesystem == "workspace:write":
            writes.append(str(workspace))
    return Grant(
        plugin_id=manifest.id,
        enforce=tier != "core-bundled",
        network=bool(perms.network),
        hosts=tuple(perms.network),
        shell=bool(perms.shell),
        write_roots=tuple(r for r in (_real(p) for p in writes) if r),
        read_roots=tuple(r for r in (_real(p) for p in reads) if r),
        temp_roots=tuple(_temp_roots()),
        mira_home=_real(data_root) or "",
    )


def _violation(grant: Grant, kind: str, target: str) -> None:
    message = f"plugin {grant.plugin_id} may not {kind} {target}: not declared in its permissions"
    if grant.enforce:
        raise PermissionError(message)
    key = (kind, target)
    if key not in grant._reported:
        grant._reported.add(key)
        logger.warning("plugin permission audit: {}", message)


def _in_home(grant: Grant, real: str) -> bool:
    return bool(grant.mira_home) and _under(real, (grant.mira_home,))


def _check_write(grant: Grant, path: Any) -> None:
    real = _real(path)
    if real is None or _under(real, grant.write_roots):
        return
    # The temp dir is fine — unless the Mira home lives in it (CI, tests):
    # the home's own rules come first.
    if _under(real, grant.temp_roots) and not _in_home(grant, real):
        return
    _violation(grant, "write", real)


def _check_read(grant: Grant, path: Any) -> None:
    real = _real(path)
    if real is None or not _in_home(grant, real):
        return
    if _under(real, grant.read_roots) or _under(real, grant.write_roots):
        return
    _violation(grant, "read", real)


def _is_write_open(mode: Any, flags: Any) -> bool:
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _inet_host(address: Any) -> str | None:
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        return address[0]
    return None


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in _WATCHED:
        return
    grant = _current.get()
    if grant is None or getattr(_inside, "busy", False):
        return
    _inside.busy = True
    try:
        _check(grant, event, args)
    finally:
        _inside.busy = False


def _check(grant: Grant, event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if _is_write_open(mode, flags):
            _check_write(grant, path)
        else:
            _check_read(grant, path)
    elif event in ("os.remove", "os.mkdir", "os.rmdir", "shutil.rmtree"):
        _check_write(grant, args[0] if args else None)
    elif event == "os.rename":
        for path in args[:2]:
            _check_write(grant, path)
    elif event == "sqlite3.connect":
        database = args[0] if args else None
        if database not in (None, ":memory:", b":memory:", ""):
            _check_write(grant, database)
    elif event == "socket.connect":
        sock, address = (list(args) + [None, None])[:2]
        family = getattr(sock, "family", None)
        if family not in (socket.AF_INET, socket.AF_INET6):
            return
        host = _inet_host(address)
        if not grant.network and host is not None:
            _violation(grant, "connect to", host)
    elif event == "socket.getaddrinfo":
        host = args[0] if args else None
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        if isinstance(host, str) and host and not _is_loopback(host):
            if not grant.network:
                _violation(grant, "resolve", host)
            elif grant.hosts and "*" not in grant.hosts and not _host_declared(host, grant.hosts):
                key = ("resolve", host)
                if key not in grant._reported:
                    grant._reported.add(key)
                    logger.info("plugin {} resolves {} (not in its declared hosts)", grant.plugin_id, host)
    elif not grant.shell:
        what = args[0] if args else event
        _violation(grant, "run", os.fsdecode(what) if isinstance(what, (str, bytes, os.PathLike)) else event)


def _host_declared(host: str, declared: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for pattern in declared:
        pattern = pattern.lower()
        if pattern == host or (pattern.startswith("*.") and host.endswith(pattern[1:])):
            return True
    return False


def install() -> None:
    """Add the audit hook (once per process; audit hooks can't be removed)."""
    global _installed
    if _installed:
        return
    with _install_lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True
