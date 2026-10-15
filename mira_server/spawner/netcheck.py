"""Verify that the socket listening on an engine port belongs to the user.

Every local account can listen on loopback, so another user could squat the
port an engine reported and receive the engine token. Before the first proxied
request to a port, Mira Server checks the owner of every loopback (or
wildcard) LISTEN socket on it:

- Linux: ``/proc/net/tcp`` and ``/proc/net/tcp6`` (world-readable; the uid
  column is the socket owner). Fails closed.
- Elsewhere (macOS dev mode): psutil, checking the reported pid's uid and that
  it listens on the port.
"""

from __future__ import annotations

import ipaddress
import sys
from dataclasses import dataclass
from pathlib import Path

_TCP_LISTEN = "0A"


@dataclass(frozen=True)
class Listener:
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int
    uid: int
    inode: int


@dataclass(frozen=True)
class ListenerCheck:
    ok: bool
    reason: str = ""
    uids: tuple[int, ...] = ()


def _decode_address(hex_addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    raw = bytes.fromhex(hex_addr)
    if len(raw) == 4:
        return ipaddress.IPv4Address(raw[::-1])
    if len(raw) == 16:
        # Four host-order (little-endian on x86/arm) 32-bit words.
        return ipaddress.IPv6Address(b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
    raise ValueError(f"bad address {hex_addr!r}")


def parse_proc_net_tcp(text: str) -> list[Listener]:
    """LISTEN sockets from the contents of ``/proc/net/tcp`` or ``/proc/net/tcp6``."""
    listeners: list[Listener] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != _TCP_LISTEN:
            continue
        try:
            addr_hex, port_hex = fields[1].split(":")
            listeners.append(Listener(_decode_address(addr_hex), int(port_hex, 16), int(fields[7]),
                                      int(fields[9])))
        except ValueError:
            continue
    return listeners


def _reaches_loopback(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether a connection to 127.0.0.1/::1 on the port could land on this socket."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def listeners_on_port(port: int, *, proc_root: Path | str = "/proc") -> list[Listener]:
    found: list[Listener] = []
    for name in ("tcp", "tcp6"):
        try:
            text = (Path(proc_root) / "net" / name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        found.extend(item for item in parse_proc_net_tcp(text) if item.port == port)
    return found


def check_proc_listener(port: int, expected_uid: int, *, proc_root: Path | str = "/proc") -> ListenerCheck:
    listeners = [item for item in listeners_on_port(port, proc_root=proc_root) if _reaches_loopback(item.address)]
    if not listeners:
        return ListenerCheck(False, f"nothing listens on loopback port {port}")
    uids = tuple(sorted({item.uid for item in listeners}))
    if uids != (expected_uid,):
        return ListenerCheck(False, f"port {port} is held by uid(s) {list(uids)}, expected {expected_uid}", uids)
    return ListenerCheck(True, uids=uids)


def check_psutil_listener(port: int, expected_uid: int, pid: int | None) -> ListenerCheck:
    import psutil

    if pid is None:
        return ListenerCheck(False, "the engine reported no pid to verify")
    try:
        proc = psutil.Process(pid)
        uid = proc.uids().effective
        connections = proc.net_connections(kind="inet")
    except psutil.Error as exc:
        return ListenerCheck(False, f"cannot inspect pid {pid}: {exc}")
    if uid != expected_uid:
        return ListenerCheck(False, f"pid {pid} runs as uid {uid}, expected {expected_uid}", (uid,))
    for conn in connections:
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
            return ListenerCheck(True, uids=(uid,))
    return ListenerCheck(False, f"pid {pid} does not listen on port {port}", (uid,))


def check_listener(port: int, expected_uid: int, *, pid: int | None = None,
                   platform: str | None = None, proc_root: Path | str = "/proc") -> ListenerCheck:
    if (platform or sys.platform).startswith("linux"):
        return check_proc_listener(port, expected_uid, proc_root=proc_root)
    return check_psutil_listener(port, expected_uid, pid)


def user_slice_of(pid: int, *, proc_root: Path | str = "/proc") -> str | None:
    """The ``user-<uid>.slice`` the process runs in, or None (outside one, or unknown).

    Engines Mira Server starts run there when sudo opens a logind session for
    ``engine-launch start``; per-user systemd limits apply only then.
    """
    try:
        text = (Path(proc_root) / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        path = line.split(":", 2)[-1]
        for part in path.split("/"):
            if part.startswith("user-") and part.endswith(".slice") and part[5:-6].isdigit():
                return part
    return None
