"""Mira Server: signs host users in and runs one Mira engine per user.

Mira Server authenticates people against the host's accounts (PAM), starts
each user's engine as that user's own uid through a sudo launcher that reuses
the Remote-SSH contract (``mira remote attach --json``), and reverse-proxies
``/api/*`` and ``/ws`` to it. See ``docs/server-edition/mira-server.md``.

This package deliberately does not import ``mira_engine``: the engine runs in
separate processes, and importing it here would pull in the whole agent stack.
"""

from __future__ import annotations

from importlib import metadata as _metadata

PRODUCT = "mira-server"
PROTOCOL = 1


def server_version() -> str:
    try:
        return _metadata.version("mira-engine")
    except _metadata.PackageNotFoundError:
        return "0.0.0"


__all__ = ["PRODUCT", "PROTOCOL", "server_version"]
