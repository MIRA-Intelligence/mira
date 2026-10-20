"""Engine spawners (sudo launcher in production, local in dev) and listener checks."""

from __future__ import annotations

from mira_server.config import SpawnerSection
from mira_server.spawner.base import EngineHandle, Spawner, SpawnerError


def make_spawner(section: SpawnerSection) -> Spawner:
    if section.backend == "local":
        from mira_server.spawner.local import LocalSpawner

        return LocalSpawner.from_config(section)
    from mira_server.spawner.sudo import SudoSpawner

    return SudoSpawner.from_config(section)


__all__ = ["EngineHandle", "Spawner", "SpawnerError", "make_spawner"]
