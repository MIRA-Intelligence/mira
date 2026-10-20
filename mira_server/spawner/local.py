"""Dev spawner: runs ``mira remote ...`` directly as the current OS user.

There is no isolation: every dev user's engine runs as you. Set
``spawner.local_mira_home = "/tmp/mira-dev/{user}"`` to give each dev user a
separate MIRA_HOME (and therefore a separate engine).
"""

from __future__ import annotations

import os
import sys

from mira_server.auth.identity import Identity
from mira_server.config import SpawnerSection
from mira_server.spawner.base import ACTION_ARGS, INTERNAL, LauncherSpawner, SpawnerError


class LocalSpawner(LauncherSpawner):
    def __init__(self, *, engine_python: str = "", mira_home_template: str = "",
                 start_timeout: float, stop_timeout: float) -> None:
        super().__init__(start_timeout=start_timeout, stop_timeout=stop_timeout)
        self.engine_python = engine_python or sys.executable
        self.mira_home_template = mira_home_template

    @classmethod
    def from_config(cls, section: SpawnerSection) -> LocalSpawner:
        return cls(engine_python=section.engine_python, mira_home_template=section.local_mira_home,
                   start_timeout=section.start_timeout, stop_timeout=section.stop_timeout)

    def mira_home(self, identity: Identity) -> str | None:
        if not self.mira_home_template:
            return None
        return os.path.expanduser(self.mira_home_template.replace("{user}", identity.name))

    def command(self, identity: Identity, action: str) -> tuple[list[str], dict[str, str]]:
        if action not in ACTION_ARGS:
            raise SpawnerError(INTERNAL, f"unknown launcher action {action!r}")
        env = dict(os.environ)
        env.pop("MIRA_GATEWAY_TOKEN", None)
        home = self.mira_home(identity)
        if home:
            env["MIRA_HOME"] = home
        return [self.engine_python, "-m", "mira_engine", "remote", *ACTION_ARGS[action]], env

    def expected_uid(self, identity: Identity) -> int:
        return os.getuid()
