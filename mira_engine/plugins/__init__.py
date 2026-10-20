"""Plugin kernel (design §3, mira#206-#209): one manifest, one fault-tolerant
loader and runtime guards for every kind of extension — channels, tools, hooks
and skills — used for first-party and community plugins alike."""

from mira_engine.plugins.api import PLUGIN_API_VERSION, satisfies
from mira_engine.plugins.loader import (
    LoadedPlugin,
    PluginRuntime,
    PluginSource,
    get_runtime,
    set_runtime_for_tests,
)
from mira_engine.plugins.manifest import ManifestError, PluginManifest, load_manifest, parse_manifest

__all__ = [
    "PLUGIN_API_VERSION",
    "LoadedPlugin",
    "ManifestError",
    "PluginManifest",
    "PluginRuntime",
    "PluginSource",
    "get_runtime",
    "load_manifest",
    "parse_manifest",
    "satisfies",
    "set_runtime_for_tests",
]
