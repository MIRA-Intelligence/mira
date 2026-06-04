"""Plugin manager for pluggable skill packs."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.config.paths import get_workspace_path
from mira_engine.utils.helpers import ensure_dir, get_mira_dir

PLUGIN_MANIFEST_FILENAME = "plugin.json"
_PLUGIN_SOURCE_FILENAME = ".mira-plugin-source.json"
_GLOBAL_STATE_FILENAME = "plugin_state.json"
_PROJECT_OVERRIDES_FILENAME = "plugin_overrides.json"
_BUILTIN_PLUGIN_ID = "builtin-skills"
_BUILTIN_PLUGIN_NAME = "Built-in Skills"
# Directories skipped when scanning built-in skills (keeps startup/interrupt responsive).
_BUILTIN_SCAN_SKIP_DIRS = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "htmlcov",
})


class SkillPluginError(ValueError):
    """Raised for invalid plugin state or plugin package errors."""


def _is_valid_identifier(value: str) -> bool:
    if not value:
        return False
    if not (value[0].isalnum()):
        return False
    return all(ch.isalnum() or ch in "._-" for ch in value)


def _safe_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _is_explicit(state_entry: Any, category: str, key: str) -> bool:
    if not isinstance(state_entry, dict):
        return False
    collection = state_entry.get(category)
    return isinstance(collection, dict) and key in collection and isinstance(collection[key], bool)


def _effective_scope_state(
    global_value: bool | None,
    project_value: bool | None,
    *,
    global_explicit: bool = False,
    project_explicit: bool = False,
    default: bool = True,
) -> dict[str, bool | None]:
    global_enabled = default if global_value is None else global_value
    effective_enabled = global_enabled if project_value is None else project_value
    return {
        "global": global_enabled,
        "project": project_value,
        "effective": effective_enabled,
        "global_explicit": global_explicit,
        "project_explicit": project_explicit,
    }


class SkillPluginManager:
    """Manage skill plugins and scope-aware enable/disable state."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        global_workspace = get_workspace_path(None)
        self.global_skills_dir = ensure_dir(get_mira_dir(global_workspace) / "skills")
        self.project_skills_dir = ensure_dir(get_mira_dir(workspace) / "skills")
        self.plugins_root = ensure_dir(self.global_skills_dir / "plugins")
        self.global_state_path = self.global_skills_dir / _GLOBAL_STATE_FILENAME
        self.project_overrides_path = self.project_skills_dir / _PROJECT_OVERRIDES_FILENAME
        self.builtin_skills_dir = Path(__file__).parent.parent / "skills"
        self._builtin_manifest_cache: dict[str, Any] | None | object = object()

    def warm_builtin_manifest_cache(self) -> None:
        """Build and cache the built-in skill manifest (safe to call from a worker thread)."""
        if self._builtin_manifest_cache is object():
            self._builtin_manifest_cache = self._build_builtin_manifest()

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_state(self, path: Path) -> dict[str, Any]:
        raw = self._read_json(path)
        plugins_raw = raw.get("plugins")
        if not isinstance(plugins_raw, dict):
            return {"plugins": {}}
        plugins: dict[str, Any] = {}
        for plugin_id, plugin_state in plugins_raw.items():
            if not isinstance(plugin_id, str) or not isinstance(plugin_state, dict):
                continue
            groups_raw = plugin_state.get("groups")
            skills_raw = plugin_state.get("skills")
            groups = {}
            skills = {}
            if isinstance(groups_raw, dict):
                groups = {
                    group_id: value
                    for group_id, value in groups_raw.items()
                    if isinstance(group_id, str) and isinstance(value, bool)
                }
            if isinstance(skills_raw, dict):
                skills = {
                    skill_id: value
                    for skill_id, value in skills_raw.items()
                    if isinstance(skill_id, str) and isinstance(value, bool)
                }
            plugins[plugin_id] = {
                "enabled": _safe_bool(plugin_state.get("enabled")),
                "groups": groups,
                "skills": skills,
            }
        return {"plugins": plugins}

    def _write_state(self, path: Path, state: dict[str, Any]) -> None:
        self._write_json(path, state)

    def _iter_plugin_dirs(self) -> list[Path]:
        if not self.plugins_root.exists():
            return []
        return sorted(
            (
                p for p in self.plugins_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ),
            key=lambda p: p.name,
        )

    def _build_builtin_manifest(self) -> dict[str, Any] | None:
        if self._builtin_manifest_cache is not object():
            cached = self._builtin_manifest_cache
            return cached if isinstance(cached, dict) else None

        root = self.builtin_skills_dir
        if not root.is_dir():
            self._builtin_manifest_cache = None
            return None

        skills: list[dict[str, Any]] = []
        groups_map: dict[str, set[str]] = {}
        seen: set[str] = set()
        skill_files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _BUILTIN_SCAN_SKIP_DIRS and not d.startswith(".")
            ]
            if "SKILL.md" in filenames:
                skill_files.append(Path(dirpath) / "SKILL.md")
        for skill_file in sorted(skill_files):
            rel = skill_file.relative_to(root)
            parts = rel.parts
            if len(parts) < 2:
                continue
            skill_id = parts[-2]
            if not _is_valid_identifier(skill_id) or skill_id in seen:
                continue
            group_id = parts[0] if len(parts) >= 3 else "general"
            if not _is_valid_identifier(group_id):
                group_id = "general"
            seen.add(skill_id)
            groups_map.setdefault(group_id, set()).add(skill_id)
            skills.append({
                "id": skill_id,
                "name": skill_id,
                "relative_path": str(rel),
                "group_ids": [group_id],
            })

        if not skills:
            self._builtin_manifest_cache = None
            return None

        groups = [
            {
                "id": group_id,
                "name": group_id.replace("-", " ").replace("_", " ").title(),
                "skill_ids": sorted(skill_ids),
            }
            for group_id, skill_ids in sorted(groups_map.items(), key=lambda item: item[0])
        ]
        manifest = {
            "id": _BUILTIN_PLUGIN_ID,
            "name": _BUILTIN_PLUGIN_NAME,
            "version": "1.0.0",
            "description": "Skills bundled with Mira.",
            "install_path": str(root),
            "groups": groups,
            "skills": skills,
        }
        self._builtin_manifest_cache = manifest
        return manifest

    def _iter_plugin_records(self) -> list[tuple[dict[str, Any], Path, dict[str, str]]]:
        records: list[tuple[dict[str, Any], Path, dict[str, str]]] = []
        self.warm_builtin_manifest_cache()
        builtin = (
            self._builtin_manifest_cache
            if isinstance(self._builtin_manifest_cache, dict)
            else None
        )
        if builtin is not None:
            records.append(
                (
                    builtin,
                    self.builtin_skills_dir,
                    {"type": "builtin", "path": str(self.builtin_skills_dir)},
                )
            )
        for plugin_dir in self._iter_plugin_dirs():
            try:
                manifest = self._load_manifest(plugin_dir)
            except SkillPluginError as exc:
                logger.warning("Skip invalid skill plugin {}: {}", plugin_dir, exc)
                continue
            source = self._read_plugin_source(plugin_dir)
            records.append((manifest, plugin_dir, source))
        return records

    def _read_plugin_source(self, plugin_dir: Path) -> dict[str, str]:
        source_file = plugin_dir / _PLUGIN_SOURCE_FILENAME
        source = self._read_json(source_file)
        source_type = source.get("type")
        source_path = source.get("path")
        if isinstance(source_type, str) and isinstance(source_path, str):
            return {"type": source_type, "path": source_path}
        return {"type": "directory", "path": str(plugin_dir)}

    def _write_plugin_source(self, plugin_dir: Path, source_type: str, source_path: str) -> None:
        self._write_json(
            plugin_dir / _PLUGIN_SOURCE_FILENAME,
            {"type": source_type, "path": source_path},
        )

    def _discover_skills(self, plugin_dir: Path) -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        scanned_dirs: list[Path] = []
        candidate_root = plugin_dir / "skills"
        if candidate_root.is_dir():
            scanned_dirs.append(candidate_root)
        scanned_dirs.append(plugin_dir)
        seen: set[str] = set()
        for root in scanned_dirs:
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                skill_file = child / "SKILL.md"
                if not skill_file.is_file():
                    continue
                skill_id = child.name
                if not _is_valid_identifier(skill_id) or skill_id in seen:
                    continue
                seen.add(skill_id)
                discovered.append({
                    "id": skill_id,
                    "name": skill_id,
                    "relative_path": str(skill_file.relative_to(plugin_dir)),
                    "group_ids": [],
                })
        return discovered

    def _normalize_identifier(self, raw: str, fallback: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip().lower())
        normalized = normalized.strip("-._")
        if not normalized:
            normalized = fallback
        if not normalized[0].isalnum():
            normalized = f"{fallback}-{normalized}".strip("-._")
        return normalized

    def _read_skill_name_from_frontmatter(self, skill_file: Path) -> str | None:
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            return None
        if not content.startswith("---"):
            return None
        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
        if not match:
            return None
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().lower() != "name":
                continue
            name = value.strip().strip("\"'")
            return name or None
        return None

    def _infer_manifest_payload(
        self,
        plugin_dir: Path,
        *,
        plugin_id_hint: str,
        plugin_name_hint: str,
    ) -> dict[str, Any]:
        skill_files = sorted([p for p in plugin_dir.rglob("SKILL.md") if p.is_file()])
        if not skill_files:
            raise SkillPluginError(f"No SKILL.md found in package: {plugin_dir}")

        plugin_id = self._normalize_identifier(plugin_id_hint, "skill-plugin")
        if not _is_valid_identifier(plugin_id):
            raise SkillPluginError(f"Unable to derive valid plugin id from: {plugin_id_hint}")

        skills: list[dict[str, Any]] = []
        groups: dict[str, set[str]] = {}
        used_ids: set[str] = set()

        for skill_file in skill_files:
            rel_actual = skill_file.relative_to(plugin_dir)
            parts = rel_actual.parts
            if len(parts) < 2:
                continue

            skill_dir_rel = skill_file.parent.relative_to(plugin_dir)
            skill_folder = parts[-2]
            # Group pattern: <group_folder>/<skill_folder>/SKILL.md
            # We also accept an optional wrapper prefix in zip paths by using
            # the last 3 path segments.
            if len(parts) >= 3:
                group_id = self._normalize_identifier(parts[-3], "group")
            else:
                group_id = None

            display_name = self._read_skill_name_from_frontmatter(skill_file) or skill_folder
            skill_id = self._normalize_identifier(display_name, "skill")
            if skill_id in used_ids:
                skill_id = self._normalize_identifier("-".join(skill_dir_rel.parts), skill_id)
            suffix = 2
            while skill_id in used_ids:
                skill_id = f"{skill_id}-{suffix}"
                suffix += 1
            used_ids.add(skill_id)

            skill_entry: dict[str, Any] = {
                "id": skill_id,
                "path": str(skill_dir_rel),
                "name": display_name,
            }
            if group_id and _is_valid_identifier(group_id):
                skill_entry["groups"] = [group_id]
                groups.setdefault(group_id, set()).add(skill_id)
            skills.append(skill_entry)

        if not skills:
            raise SkillPluginError(f"No valid SKILL.md entries found in package: {plugin_dir}")

        payload: dict[str, Any] = {
            "id": plugin_id,
            "name": plugin_name_hint or plugin_id,
            "version": "0.1.0",
            "description": "Auto-generated manifest from local skill package.",
            "skills": skills,
        }
        if groups:
            payload["groups"] = [
                {
                    "id": gid,
                    "name": gid.replace("-", " ").replace("_", " ").title(),
                    "skills": sorted(skill_ids),
                }
                for gid, skill_ids in sorted(groups.items(), key=lambda item: item[0])
            ]
        return payload

    def _ensure_manifest_for_install(
        self,
        source_dir: Path,
        *,
        plugin_id_hint: str,
        plugin_name_hint: str,
    ) -> dict[str, Any]:
        manifest_file = source_dir / PLUGIN_MANIFEST_FILENAME
        if not manifest_file.is_file():
            payload = self._infer_manifest_payload(
                source_dir,
                plugin_id_hint=plugin_id_hint,
                plugin_name_hint=plugin_name_hint,
            )
            self._write_json(manifest_file, payload)
        return self._load_manifest(source_dir)

    def _validate_skill_path(self, plugin_dir: Path, raw_path: str) -> str:
        base = plugin_dir.resolve()
        candidate = (plugin_dir / raw_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise SkillPluginError(f"Skill path escapes plugin directory: {raw_path}") from exc
        if candidate.is_dir():
            candidate = candidate / "SKILL.md"
        if candidate.name != "SKILL.md" or not candidate.is_file():
            raise SkillPluginError(f"Skill path missing SKILL.md: {raw_path}")
        return str(candidate.relative_to(plugin_dir))

    def _infer_group_from_relative_path(self, relative_path: str) -> str | None:
        parts = Path(relative_path).parts
        if len(parts) < 3:
            return None
        # <group>/<skill>/SKILL.md or <wrapper>/<group>/<skill>/SKILL.md
        candidate = parts[-3]
        if candidate.lower() == "skills":
            return None
        group_id = self._normalize_identifier(candidate, "group")
        if not _is_valid_identifier(group_id):
            return None
        return group_id

    def _load_manifest(self, plugin_dir: Path) -> dict[str, Any]:
        manifest_file = plugin_dir / PLUGIN_MANIFEST_FILENAME
        if not manifest_file.is_file():
            raise SkillPluginError(f"Missing {PLUGIN_MANIFEST_FILENAME} in {plugin_dir}")

        try:
            manifest_raw = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SkillPluginError(f"Invalid plugin manifest: {manifest_file}") from exc
        if not isinstance(manifest_raw, dict):
            raise SkillPluginError("Plugin manifest must be a JSON object")

        plugin_id = manifest_raw.get("id")
        if not isinstance(plugin_id, str) or not _is_valid_identifier(plugin_id):
            raise SkillPluginError("Plugin manifest requires a valid string id")

        version = manifest_raw.get("version")
        if version is None:
            version = "0.1.0"
        if not isinstance(version, str):
            raise SkillPluginError("Plugin version must be a string")

        name = manifest_raw.get("name") or plugin_id
        if not isinstance(name, str):
            raise SkillPluginError("Plugin name must be a string")

        description = manifest_raw.get("description") or ""
        if not isinstance(description, str):
            raise SkillPluginError("Plugin description must be a string")

        skills: list[dict[str, Any]] = []
        skills_raw = manifest_raw.get("skills")
        if skills_raw is None:
            skills = self._discover_skills(plugin_dir)
        elif isinstance(skills_raw, list):
            seen_skill_ids: set[str] = set()
            for entry in skills_raw:
                if isinstance(entry, str):
                    skill_id = entry
                    skill_path = entry
                    skill_name = entry
                    groups_raw: list[str] = []
                elif isinstance(entry, dict):
                    skill_id = entry.get("id")
                    skill_path = entry.get("path", skill_id)
                    skill_name = entry.get("name", skill_id)
                    groups_raw = entry.get("groups") if isinstance(entry.get("groups"), list) else []
                else:
                    raise SkillPluginError("Each skill entry must be a string or object")

                if not isinstance(skill_id, str) or not _is_valid_identifier(skill_id):
                    raise SkillPluginError("Skill id must be a valid identifier")
                if skill_id in seen_skill_ids:
                    raise SkillPluginError(f"Duplicate skill id: {skill_id}")
                seen_skill_ids.add(skill_id)

                if not isinstance(skill_path, str) or not skill_path.strip():
                    raise SkillPluginError(f"Skill path missing for {skill_id}")
                if not isinstance(skill_name, str):
                    raise SkillPluginError(f"Skill name must be string for {skill_id}")

                group_ids = [g for g in groups_raw if isinstance(g, str)]
                for group_id in group_ids:
                    if not _is_valid_identifier(group_id):
                        raise SkillPluginError(f"Invalid group id reference in skill {skill_id}: {group_id}")

                relative_path = self._validate_skill_path(plugin_dir, skill_path)
                skills.append({
                    "id": skill_id,
                    "name": skill_name or skill_id,
                    "relative_path": relative_path,
                    "group_ids": sorted(set(group_ids)),
                })
        else:
            raise SkillPluginError("Plugin manifest 'skills' must be a list")

        if not skills:
            raise SkillPluginError("Plugin must expose at least one skill")

        skill_map = {skill["id"]: skill for skill in skills}

        groups: list[dict[str, Any]] = []
        groups_raw = manifest_raw.get("groups")
        if groups_raw is None:
            groups_raw = []
        if not isinstance(groups_raw, list):
            raise SkillPluginError("Plugin manifest 'groups' must be a list")

        seen_group_ids: set[str] = set()
        for entry in groups_raw:
            if not isinstance(entry, dict):
                raise SkillPluginError("Each group entry must be an object")
            group_id = entry.get("id")
            if not isinstance(group_id, str) or not _is_valid_identifier(group_id):
                raise SkillPluginError("Group id must be a valid identifier")
            if group_id in seen_group_ids:
                raise SkillPluginError(f"Duplicate group id: {group_id}")
            seen_group_ids.add(group_id)

            group_name = entry.get("name") or group_id
            if not isinstance(group_name, str):
                raise SkillPluginError(f"Group name must be string for {group_id}")
            group_skills_raw = entry.get("skills")
            if not isinstance(group_skills_raw, list):
                raise SkillPluginError(f"Group {group_id} requires a skills list")
            group_skills = []
            for skill_id in group_skills_raw:
                if not isinstance(skill_id, str):
                    raise SkillPluginError(f"Group {group_id} contains non-string skill id")
                if skill_id not in skill_map:
                    raise SkillPluginError(f"Group {group_id} references missing skill {skill_id}")
                group_skills.append(skill_id)
                skill_map[skill_id]["group_ids"] = sorted(set([*skill_map[skill_id]["group_ids"], group_id]))

            groups.append({
                "id": group_id,
                "name": group_name,
                "skill_ids": sorted(set(group_skills)),
            })

        # Backward compatibility: if old manifests omitted `groups`,
        # infer from skill path layout for grouped packages.
        if not groups:
            inferred_groups: dict[str, set[str]] = {}
            for skill in skills:
                if skill["group_ids"]:
                    continue
                inferred_group = self._infer_group_from_relative_path(skill["relative_path"])
                if inferred_group is None:
                    continue
                skill["group_ids"] = [inferred_group]
                inferred_groups.setdefault(inferred_group, set()).add(skill["id"])
            for group_id, skill_ids in sorted(inferred_groups.items(), key=lambda item: item[0]):
                seen_group_ids.add(group_id)
                groups.append({
                    "id": group_id,
                    "name": group_id.replace("-", " ").replace("_", " ").title(),
                    "skill_ids": sorted(skill_ids),
                })

        for skill in skills:
            for group_id in skill["group_ids"]:
                if group_id not in seen_group_ids:
                    raise SkillPluginError(
                        f"Skill {skill['id']} references undefined group {group_id}",
                    )

        return {
            "id": plugin_id,
            "name": name,
            "version": version,
            "description": description,
            "install_path": str(plugin_dir),
            "groups": groups,
            "skills": skills,
        }

    def _find_extracted_plugin_root(self, extracted_dir: Path) -> Path:
        direct_manifest = extracted_dir / PLUGIN_MANIFEST_FILENAME
        if direct_manifest.is_file():
            return extracted_dir

        candidates = sorted(
            [
                p for p in extracted_dir.iterdir()
                if p.is_dir() and (p / PLUGIN_MANIFEST_FILENAME).is_file()
            ],
            key=lambda p: p.name,
        )
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise SkillPluginError("Zip archive contains multiple plugin roots")

        # Manifest is optional for local skill packages: infer directly from SKILL.md layout.
        if any(item.name == "SKILL.md" for item in extracted_dir.rglob("SKILL.md")):
            return extracted_dir
        raise SkillPluginError("Zip archive contains no SKILL.md files")

    def _safe_extract_zip(self, archive_path: Path, target_dir: Path) -> None:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                entry_path = Path(info.filename)
                if entry_path.is_absolute() or ".." in entry_path.parts:
                    raise SkillPluginError(f"Unsafe zip path: {info.filename}")
            zf.extractall(target_dir)

    def _install_from_source_dir(
        self,
        source_dir: Path,
        *,
        source_type: str,
        source_path: str,
        plugin_id_hint: str,
        plugin_name_hint: str,
    ) -> dict[str, Any]:
        manifest = self._ensure_manifest_for_install(
            source_dir,
            plugin_id_hint=plugin_id_hint,
            plugin_name_hint=plugin_name_hint,
        )
        plugin_id = manifest["id"]
        destination = self.plugins_root / plugin_id
        staging = self.plugins_root / f".tmp-{plugin_id}-{int(time.time() * 1000)}"

        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source_dir, staging)
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
        self._write_plugin_source(destination, source_type=source_type, source_path=source_path)

        installed_manifest = self._load_manifest(destination)
        installed_manifest["source"] = {"type": source_type, "path": source_path}
        return installed_manifest

    def install_from_directory(self, source_dir: Path) -> dict[str, Any]:
        resolved = source_dir.expanduser().resolve()
        if not resolved.is_dir():
            raise SkillPluginError(f"Plugin source directory not found: {resolved}")
        return self._install_from_source_dir(
            resolved,
            source_type="directory",
            source_path=str(resolved),
            plugin_id_hint=resolved.name,
            plugin_name_hint=resolved.name.replace("-", " ").replace("_", " ").title(),
        )

    def install_from_zip(self, archive_path: Path, archive_name_hint: str | None = None) -> dict[str, Any]:
        resolved = archive_path.expanduser().resolve()
        if not resolved.is_file():
            raise SkillPluginError(f"Plugin zip file not found: {resolved}")
        hint_stem = Path(archive_name_hint).stem if isinstance(archive_name_hint, str) and archive_name_hint.strip() else resolved.stem
        with tempfile.TemporaryDirectory(prefix="skill-plugin-", dir=self.plugins_root) as tmp:
            tmp_dir = Path(tmp)
            self._safe_extract_zip(resolved, tmp_dir)
            plugin_root = self._find_extracted_plugin_root(tmp_dir)
            return self._install_from_source_dir(
                plugin_root,
                source_type="zip",
                source_path=str(resolved),
                plugin_id_hint=hint_stem,
                plugin_name_hint=hint_stem.replace("-", " ").replace("_", " ").title(),
            )

    def uninstall(self, plugin_id: str) -> None:
        if not isinstance(plugin_id, str) or not _is_valid_identifier(plugin_id):
            raise SkillPluginError("Invalid plugin_id")
        if plugin_id == _BUILTIN_PLUGIN_ID:
            raise SkillPluginError("Built-in skills cannot be uninstalled")
        plugin_dir = self.plugins_root / plugin_id
        if not plugin_dir.is_dir():
            raise SkillPluginError(f"Plugin not installed: {plugin_id}")
        shutil.rmtree(plugin_dir)
        for state_path in (self.global_state_path, self.project_overrides_path):
            state = self._read_state(state_path)
            plugins = state.get("plugins", {})
            if plugin_id in plugins:
                plugins.pop(plugin_id, None)
                self._write_state(state_path, state)

    def set_enabled(
        self,
        *,
        scope: str,
        plugin_id: str,
        target_type: str,
        enabled: bool,
        target_id: str | None = None,
    ) -> None:
        if scope not in {"global", "project"}:
            raise SkillPluginError("scope must be 'global' or 'project'")
        if target_type not in {"group", "skill"}:
            raise SkillPluginError("target_type must be 'group' or 'skill'")
        if not isinstance(plugin_id, str) or not _is_valid_identifier(plugin_id):
            raise SkillPluginError("Invalid plugin_id")
        if not isinstance(target_id, str) or not _is_valid_identifier(target_id):
            raise SkillPluginError("target_id is required for group/skill toggles")
        manifests_by_id = {record[0]["id"]: record[0] for record in self._iter_plugin_records()}
        if plugin_id not in manifests_by_id:
            raise SkillPluginError(f"Plugin not installed: {plugin_id}")
        manifest = manifests_by_id[plugin_id]
        group_to_skills = {
            group["id"]: set(group.get("skill_ids", []))
            for group in manifest.get("groups", [])
            if isinstance(group, dict) and isinstance(group.get("id"), str)
        }
        all_skills = {
            skill.get("id")
            for skill in manifest.get("skills", [])
            if isinstance(skill, dict) and isinstance(skill.get("id"), str)
        }
        if target_type == "group" and target_id not in group_to_skills:
            raise SkillPluginError(f"Unknown group for plugin {plugin_id}: {target_id}")
        if target_type == "skill" and target_id not in all_skills:
            raise SkillPluginError(f"Unknown skill for plugin {plugin_id}: {target_id}")

        state_path = self.global_state_path if scope == "global" else self.project_overrides_path
        state = self._read_state(state_path)
        plugins = state.setdefault("plugins", {})
        plugin_state = plugins.setdefault(plugin_id, {"enabled": None, "groups": {}, "skills": {}})

        if target_type == "group":
            groups = plugin_state.setdefault("groups", {})
            groups[target_id] = enabled
            # Reapplying a group clears per-skill overrides in this scope
            # so group control becomes effective again.
            skills = plugin_state.setdefault("skills", {})
            for skill_id in group_to_skills.get(target_id, set()):
                skills.pop(skill_id, None)
        else:
            skills = plugin_state.setdefault("skills", {})
            skills[target_id] = enabled

        self._write_state(state_path, state)

    def list_plugins(self) -> list[dict[str, Any]]:
        global_state = self._read_state(self.global_state_path).get("plugins", {})
        project_state = self._read_state(self.project_overrides_path).get("plugins", {})

        plugins: list[dict[str, Any]] = []
        for manifest, plugin_root, source in self._iter_plugin_records():
            plugin_id = manifest["id"]
            global_entry = global_state.get(plugin_id, {})
            project_entry = project_state.get(plugin_id, {})

            # Plugin-level toggles are deprecated; keep plugin gate always enabled.
            plugin_enabled = _effective_scope_state(
                True,
                None,
                global_explicit=False,
                project_explicit=False,
                default=True,
            )

            groups: list[dict[str, Any]] = []
            group_effective: dict[str, bool] = {}
            group_customized_global: dict[str, bool] = {}
            group_customized_project: dict[str, bool] = {}
            for group in manifest["groups"]:
                group_id = group["id"]
                global_value = None
                project_value = None
                if isinstance(global_entry, dict):
                    global_value = _safe_bool((global_entry.get("groups") or {}).get(group_id))
                if isinstance(project_entry, dict):
                    project_value = _safe_bool((project_entry.get("groups") or {}).get(group_id))
                group_enabled = _effective_scope_state(global_value, project_value)
                group_effective[group_id] = bool(group_enabled["effective"])
                group_customized_global[group_id] = False
                group_customized_project[group_id] = False
                groups.append({
                    "id": group_id,
                    "name": group["name"],
                    "skill_ids": group["skill_ids"],
                    "enabled": {
                        **group_enabled,
                        "effective": bool(plugin_enabled["effective"]) and bool(group_enabled["effective"]),
                    },
                })

            skills: list[dict[str, Any]] = []
            for skill in manifest["skills"]:
                skill_id = skill["id"]
                skill_file = plugin_root / skill["relative_path"]
                global_value = None
                project_value = None
                if isinstance(global_entry, dict):
                    global_value = _safe_bool((global_entry.get("skills") or {}).get(skill_id))
                if isinstance(project_entry, dict):
                    project_value = _safe_bool((project_entry.get("skills") or {}).get(skill_id))
                global_explicit = _is_explicit(global_entry, "skills", skill_id)
                project_explicit = _is_explicit(project_entry, "skills", skill_id)
                skill_enabled = _effective_scope_state(
                    global_value,
                    project_value,
                    global_explicit=global_explicit,
                    project_explicit=project_explicit,
                )
                for group_id in skill["group_ids"]:
                    if global_explicit:
                        group_customized_global[group_id] = True
                    if project_explicit:
                        group_customized_project[group_id] = True
                # Per-skill explicit overrides bypass group gate until group is reapplied.
                if global_explicit or project_explicit:
                    group_gate = True
                else:
                    group_gate = all(group_effective.get(group_id, True) for group_id in skill["group_ids"])
                effective_enabled = (
                    bool(plugin_enabled["effective"])
                    and bool(skill_enabled["effective"])
                    and group_gate
                )
                skills.append({
                    "id": skill_id,
                    "name": skill["name"],
                    "path": str(skill_file),
                    "group_ids": skill["group_ids"],
                    "enabled": {
                        **skill_enabled,
                        "effective": effective_enabled,
                    },
                })

            plugins.append({
                "id": plugin_id,
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest["description"],
                "install_path": manifest["install_path"],
                "source": source,
                "enabled": plugin_enabled,
                "groups": groups,
                "skills": skills,
            })
            for group in plugins[-1]["groups"]:
                group_id = group["id"]
                group["customized"] = {
                    "global": group_customized_global.get(group_id, False),
                    "project": group_customized_project.get(group_id, False),
                }
        return plugins

    def list_enabled_skills(self) -> list[dict[str, str]]:
        discovered: list[dict[str, str]] = []
        seen: set[str] = set()
        for plugin in self.list_plugins():
            for skill in plugin.get("skills", []):
                if not skill.get("enabled", {}).get("effective"):
                    continue
                name = skill.get("id")
                path = skill.get("path")
                if not isinstance(name, str) or not isinstance(path, str):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                discovered.append({
                    "name": name,
                    "path": path,
                    "source": "builtin" if plugin["id"] == _BUILTIN_PLUGIN_ID else "plugin",
                    "plugin_id": plugin["id"],
                })
        return discovered

    def get_managed_skill_names(self) -> set[str]:
        managed: set[str] = set()
        for plugin in self.list_plugins():
            for skill in plugin.get("skills", []):
                skill_id = skill.get("id")
                if isinstance(skill_id, str):
                    managed.add(skill_id)
        return managed
