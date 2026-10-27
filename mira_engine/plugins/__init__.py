"""Plugin kernel (design §3, mira#206-#209): one manifest, one fault-tolerant
loader and runtime guards for every kind of extension — channels, tools, hooks
and skills — used for first-party and community plugins alike.

Names resolve lazily (PEP 562): importing a submodule such as
``mira_engine.plugins.paths`` must not pull in the loader, which imports the
agent package and, through it, the config schema."""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "PLUGIN_API_VERSION": "api",
    "satisfies": "api",
    "LoadedPlugin": "loader",
    "PluginRuntime": "loader",
    "PluginSource": "loader",
    "get_runtime": "loader",
    "set_runtime_for_tests": "loader",
    "ManifestError": "manifest",
    "PluginManifest": "manifest",
    "load_manifest": "manifest",
    "parse_manifest": "manifest",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
