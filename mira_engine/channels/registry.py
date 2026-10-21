"""Auto-discovery for built-in channel modules and external plugins."""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from mira_engine.channels.base import BaseChannel

_INTERNAL = frozenset({"base", "manager", "registry"})


def discover_channel_names() -> list[str]:
    """Return all built-in channel module names by scanning the package (zero imports)."""
    import mira_engine.channels as pkg

    return [
        name
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__)
        if name not in _INTERNAL and not ispkg
    ]


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """Import *module_name* and return the first BaseChannel subclass found."""
    from mira_engine.channels.base import BaseChannel as _Base

    mod = importlib.import_module(f"mira_engine.channels.{module_name}")
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
            return obj
    raise ImportError(f"No BaseChannel subclass in mira_engine.channels.{module_name}")


def discover_plugins() -> dict[str, type[BaseChannel]]:
    """Discover external channel plugins registered via entry_points."""
    from importlib.metadata import entry_points

    plugins: dict[str, type[BaseChannel]] = {}
    for ep in entry_points(group="mira_engine.channels"):
        try:
            cls = ep.load()
            plugins[ep.name] = cls
        except Exception as e:
            logger.warning("Failed to load channel plugin '{}': {}", ep.name, e)
    return plugins


def discover_all() -> dict[str, type[BaseChannel]]:
    """Return all channels: built-in (pkgutil) and first-party plugin channels,
    merged with external ones (entry_points, other mira plugins).

    Built-in and first-party (core-bundled) channels take priority — an
    external plugin cannot shadow their names.
    """
    builtin: dict[str, type[BaseChannel]] = {}
    for modname in discover_channel_names():
        try:
            builtin[modname] = load_channel_class(modname)
        except ImportError as e:
            logger.debug("Skipping built-in channel '{}': {}", modname, e)

    external = discover_plugins()
    first_party: dict[str, type[BaseChannel]] = {}
    # Channels from mira plugins (mira#206): the repo's plugins/ ship the
    # messaging channels and rank as built-ins; any other plugin ranks below
    # entry points, as before.
    try:
        from mira_engine.plugins import get_runtime

        runtime = get_runtime()
        first_party = runtime.channels(core=True)
        external = {**runtime.channels(core=False), **external}
    except Exception as e:  # noqa: BLE001 - channels must come up without plugins
        logger.warning("Could not load plugin channels: {}", e)
    first_party = {**first_party, **builtin}
    shadowed = set(external) & set(first_party)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)

    return {**external, **first_party}
