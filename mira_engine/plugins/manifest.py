"""Plugin manifest (``plugin.json``) — the contract between a plugin and the kernel.

Design §3.3 (mira#206). Extends the skill-pack manifest: a manifest that only
lists ``skills`` (the pre-#206 format) is still valid.

.. code-block:: json

    {
      "id": "telegram-channel",
      "version": "1.2.0",
      "plugin_api": ">=1.0,<2.0",
      "owner": "github:alice",
      "description": "…",
      "provides": {
        "channels":  [{"name": "telegram", "entry": "telegram_channel:TelegramChannel"}],
        "tools":     [{"name": "tg_send", "entry": "tools:TgSendTool"}],
        "skills":    [{"id": "tg-howto", "path": "skills/tg-howto"}],
        "hooks":     [{"entry": "hooks:AuditHook", "mode": "observe"}]
      },
      "permissions": {"network": ["api.telegram.org"], "filesystem": "workspace:read",
                      "shell": false, "secrets": ["TELEGRAM_BOT_TOKEN"]},
      "isolation": "in-process",
      "default_enabled": false
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

MANIFEST_FILENAME = "plugin.json"

Tier = Literal["core-bundled", "verified", "community", "experimental"]
TIERS: tuple[Tier, ...] = ("core-bundled", "verified", "community", "experimental")
Isolation = Literal["in-process", "subprocess"]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ENTRY = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].*)?$")


class ManifestError(ValueError):
    """The manifest is missing, malformed, or violates the contract."""


@dataclass(frozen=True)
class EntryPoint:
    """``module:attr`` inside the plugin directory, plus its declared name."""

    entry: str
    name: str | None = None
    mode: Literal["observe", "mutate"] = "observe"


@dataclass(frozen=True)
class Permissions:
    network: tuple[str, ...] = ()
    filesystem: Literal["none", "workspace:read", "workspace:write"] = "none"
    shell: bool = False
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class PluginManifest:
    id: str
    version: str
    root: Path
    plugin_api: str = ">=1.0,<2.0"
    owner: str | None = None
    description: str = ""
    declared_tier: Tier | None = None
    channels: tuple[EntryPoint, ...] = ()
    tools: tuple[EntryPoint, ...] = ()
    hooks: tuple[EntryPoint, ...] = ()
    skills: tuple[dict[str, Any], ...] = ()
    permissions: Permissions = field(default_factory=Permissions)
    isolation: Isolation = "in-process"
    default_enabled: bool = False

    @property
    def has_code(self) -> bool:
        return bool(self.channels or self.tools or self.hooks)


def _entries(raw: Any, kind: str, *, named: bool) -> tuple[EntryPoint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"provides.{kind} must be a list")
    out: list[EntryPoint] = []
    for item in raw:
        if not isinstance(item, dict) or not _ENTRY.match(str(item.get("entry", ""))):
            raise ManifestError(f"provides.{kind}: each item needs an 'entry' of the form 'module:attr'")
        name = item.get("name")
        if named and not (isinstance(name, str) and name):
            raise ManifestError(f"provides.{kind}: each item needs a 'name'")
        mode = item.get("mode", "observe")
        if mode not in ("observe", "mutate"):
            raise ManifestError(f"provides.{kind}: mode must be observe or mutate")
        out.append(EntryPoint(entry=item["entry"], name=name, mode=mode))
    return tuple(out)


def parse_manifest(data: dict[str, Any], root: Path) -> PluginManifest:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    pid = data.get("id")
    if not isinstance(pid, str) or not _ID.match(pid):
        raise ManifestError("id must be lowercase letters, digits and dashes")
    version = data.get("version")
    if not isinstance(version, str) or not _SEMVER.match(version):
        raise ManifestError("version must be semver (e.g. 1.0.0)")

    provides = data.get("provides") or {}
    if not isinstance(provides, dict):
        raise ManifestError("provides must be an object")
    # Pre-#206 skill packs list skills at the top level.
    skills = provides.get("skills", data.get("skills")) or []
    if not isinstance(skills, list):
        raise ManifestError("skills must be a list")

    perms = data.get("permissions") or {}
    if not isinstance(perms, dict):
        raise ManifestError("permissions must be an object")
    fs = perms.get("filesystem", "none")
    if fs not in ("none", "workspace:read", "workspace:write"):
        raise ManifestError("permissions.filesystem must be none, workspace:read or workspace:write")

    tier = data.get("tier")
    if tier is not None and tier not in TIERS:
        raise ManifestError(f"tier must be one of {', '.join(TIERS)}")
    isolation = data.get("isolation", "in-process")
    if isolation not in ("in-process", "subprocess"):
        raise ManifestError("isolation must be in-process or subprocess")

    return PluginManifest(
        id=pid,
        version=version,
        root=root,
        plugin_api=str(data.get("plugin_api") or ">=1.0,<2.0"),
        owner=data.get("owner"),
        description=str(data.get("description") or ""),
        declared_tier=tier,
        channels=_entries(provides.get("channels"), "channels", named=True),
        tools=_entries(provides.get("tools"), "tools", named=True),
        hooks=_entries(provides.get("hooks"), "hooks", named=False),
        skills=tuple(s for s in skills if isinstance(s, dict)),
        permissions=Permissions(
            network=tuple(str(h) for h in perms.get("network") or ()),
            filesystem=fs,
            shell=bool(perms.get("shell", False)),
            secrets=tuple(str(s) for s in perms.get("secrets") or ()),
        ),
        isolation=isolation,
        default_enabled=bool(data.get("default_enabled", False)),
    )


def load_manifest(plugin_dir: Path) -> PluginManifest:
    path = plugin_dir / MANIFEST_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ManifestError(f"cannot read {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ManifestError(f"{path} is not valid JSON: {e}") from e
    return parse_manifest(data, plugin_dir)
