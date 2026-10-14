"""Production spawner: ``sudo -n -u <user> <launcher> start|status|stop``."""

from __future__ import annotations

from pathlib import Path

from mira_server.auth.identity import Identity
from mira_server.config import SpawnerSection, valid_username
from mira_server.spawner.base import ACTION_ARGS, INTERNAL, LauncherSpawner, SpawnerError


class SudoSpawner(LauncherSpawner):
    def __init__(self, *, sudo: Path, launcher: Path, start_timeout: float, stop_timeout: float) -> None:
        super().__init__(start_timeout=start_timeout, stop_timeout=stop_timeout)
        self.sudo = sudo
        self.launcher = launcher

    @classmethod
    def from_config(cls, section: SpawnerSection) -> SudoSpawner:
        return cls(sudo=section.sudo, launcher=section.launcher,
                   start_timeout=section.start_timeout, stop_timeout=section.stop_timeout)

    def command(self, identity: Identity, action: str) -> tuple[list[str], dict[str, str]]:
        if action not in ACTION_ARGS:
            raise SpawnerError(INTERNAL, f"unknown launcher action {action!r}")
        if not valid_username(identity.name):
            raise SpawnerError(INTERNAL, "refusing an invalid user name")
        argv = [str(self.sudo), "-n", "-u", identity.name, "--", str(self.launcher), action]
        # sudo resets the environment anyway; the launcher builds its own.
        return argv, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}

    def expected_uid(self, identity: Identity) -> int:
        return identity.uid
