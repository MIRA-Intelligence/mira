"""Every installed plugin, read from its manifest alone (design §3.6).

The runtime (:mod:`.loader`) imports plugin code and is built once per process.
The catalog only parses ``plugin.json`` files and the plugin state, so code
that must not import plugins or the config — the skill pack manager behind
each UI request, provider lookups while the config is being validated — can
still see which plugins exist and which are switched on.

Tiers here don't consult the config: a user plugin is ``experimental`` unless
the platform registry rated it (``plugins.default_user_tier`` only affects how
the runtime loads code).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mira_engine.plugins.manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    PluginManifest,
    Tier,
    load_manifest,
)
from mira_engine.plugins.paths import bundled_plugins_dir, user_plugins_dir
from mira_engine.plugins.state import PluginState, default_state_path

Source = Literal["bundled", "user"]


@dataclass(frozen=True)
class CatalogEntry:
    manifest: PluginManifest
    source: Source
    tier: Tier
    active: bool
    reason: str = ""

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def root(self) -> Path:
        return self.manifest.root


def plugin_dirs(root: Path | None) -> list[Path]:
    """The plugin directories under ``root`` (those with a manifest), sorted.
    Dot-prefixed directories are an installer's staging area, not plugins."""
    if root is None:
        return []
    try:
        return sorted(
            p for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and (p / MANIFEST_FILENAME).is_file()
        )
    except OSError:
        return []


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def sources() -> list[tuple[Path | None, Source]]:
    return [(bundled_plugins_dir(), "bundled"), (user_plugins_dir(), "user")]


def fingerprint(srcs: list[tuple[Path | None, Source]] | None = None, state_path: Path | None = None) -> tuple:
    """Changes whenever a manifest, the set of plugins or the plugin state does."""
    srcs = srcs if srcs is not None else sources()
    state_path = state_path or default_state_path()
    parts: list[object] = [str(state_path), _stamp(state_path)]
    for root, _ in srcs:
        for d in plugin_dirs(root):
            parts.append((str(d), _stamp(d / MANIFEST_FILENAME)))
    return tuple(parts)


def _entry(manifest: PluginManifest, source: Source, state: PluginState) -> CatalogEntry:
    if source == "bundled":
        tier: Tier = "core-bundled"
    else:
        rated = state.registry_tier(manifest.id)
        tier = rated if rated else "experimental"  # type: ignore[assignment]
    override = state.enabled_override(manifest.id)
    enabled = override if override is not None else (source == "bundled" or manifest.default_enabled)
    remote = state.remote_reason(manifest.id, manifest.version)
    tripped = state.tripped(manifest.id)
    if remote:
        return CatalogEntry(manifest, source, tier, False, f"disabled by the plugin registry: {remote}")
    if tripped:
        return CatalogEntry(manifest, source, tier, False, "disabled after repeated failures")
    if not enabled:
        return CatalogEntry(manifest, source, tier, False, "not enabled")
    return CatalogEntry(manifest, source, tier, True)


_lock = threading.Lock()
_cache: tuple[tuple, list[CatalogEntry]] | None = None


def entries(
    srcs: list[tuple[Path | None, Source]] | None = None,
    state_path: Path | None = None,
) -> list[CatalogEntry]:
    """All valid plugins (bundled first; the first of a duplicate id wins).
    Cached until a manifest, the plugin set or the plugin state changes."""
    global _cache
    srcs = srcs if srcs is not None else sources()
    state_path = state_path or default_state_path()
    key = fingerprint(srcs, state_path)
    with _lock:
        if _cache is not None and _cache[0] == key:
            return list(_cache[1])
    state = PluginState(state_path)
    out: list[CatalogEntry] = []
    seen: set[str] = set()
    for root, source in srcs:
        for d in plugin_dirs(root):
            try:
                manifest = load_manifest(d)
            except ManifestError:
                continue  # `mira plugin list` reports invalid manifests
            if manifest.id in seen:
                continue
            seen.add(manifest.id)
            out.append(_entry(manifest, source, state))
    with _lock:
        _cache = (key, out)
    return list(out)


def get(plugin_id: str) -> CatalogEntry | None:
    return next((e for e in entries() if e.id == plugin_id), None)
