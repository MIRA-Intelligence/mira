"""Project registry backed by the instance workspace file.

The registry is intentionally separate from ``config.json``. Runtime settings
describe how Mira starts; ``workspace.json`` describes which project folders a
UI instance knows about.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from mira_engine.config.paths import get_runtime_subdir
from mira_engine.utils.locks import locked_update_text, locked_write_text

PROJECT_DIR_PREFIX = "PRJ"
PROJECT_META_DIRNAME = ".mira"
PROJECT_META_FILENAME = "project.json"
PROJECT_META_SCHEMA_VERSION = 1
PROJECT_META_DEFAULT_RUN_MODE = "auto"
PROJECT_META_DEFAULT_AGENT_PROFILE = "research"
PROJECT_META_DEFAULT_CONTRACT_VERSION = 1
PROJECT_META_STRICT_CONTRACT_VERSION = 2
PROJECT_WORKSPACE_FILENAME = "workspace.json"
PROJECT_DIR_INDEX_FILENAME = "project-dirs.json"


@dataclass(frozen=True)
class ProjectRef:
    """Resolved project identity and local directory."""

    project_id: str
    project_dir: Path
    metadata: dict[str, Any]


def now_utc_iso() -> str:
    return f"{datetime.utcnow().isoformat()}Z"


def resolve_project_dir_index_path() -> Path:
    """Locate the legacy project-dir index, migrating from the legacy web name."""

    new_path = get_runtime_subdir("ui") / PROJECT_DIR_INDEX_FILENAME
    if new_path.exists():
        return new_path
    legacy_path = get_runtime_subdir("web") / PROJECT_DIR_INDEX_FILENAME
    if legacy_path.exists():
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_path), str(new_path))
            logger.info(
                "Migrated UI channel project-dir index from {} to {}",
                legacy_path,
                new_path,
            )
        except Exception:
            logger.exception("Failed to migrate legacy project-dir index")
            return legacy_path
    return new_path


def resolve_project_workspace_path() -> Path:
    """Return the instance-level workspace file path under ``.mira``."""

    return get_runtime_subdir("ui").parent / PROJECT_WORKSPACE_FILENAME


def validate_project_id(project_id: str) -> str:
    normalized = project_id.strip()
    if not normalized:
        raise ValueError("project_id required")
    if normalized in {".", ".."} or len(normalized) > 128:
        raise ValueError(
            "project_id must be 1-128 characters and cannot be '.' or '..'"
        )
    if any(ch in normalized for ch in {"/", "\\", ":", "\x00"}):
        raise ValueError("project_id cannot contain path separators or ':'")
    if any(ord(ch) < 32 for ch in normalized):
        raise ValueError("project_id cannot contain control characters")
    return normalized


def slugify_project_id(value: str, *, fallback: str = "project") -> str:
    """Convert a display name into a stable local project id."""

    text = value.strip().lower()
    text = "".join("-" if ch in {"/", "\\", ":", "\x00"} or ord(ch) < 32 else ch for ch in text)
    text = "-".join(part for part in text.split() if part)
    while "--" in text:
        text = text.replace("--", "-")
    text = text.strip(".-_")
    if not text:
        text = fallback
    return text[:128]


def _normalize_run_mode(value: Any) -> str:
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in {"manual", "auto"}:
            return mode
    return PROJECT_META_DEFAULT_RUN_MODE


def _normalize_agent_profile(value: Any) -> str:
    if isinstance(value, str):
        profile = value.strip().lower()
        if profile in {"engineer", "default", "research", "team"}:
            return profile
    return PROJECT_META_DEFAULT_AGENT_PROFILE


def _normalize_contract_version(value: Any) -> int:
    if isinstance(value, int):
        if value in {PROJECT_META_DEFAULT_CONTRACT_VERSION, PROJECT_META_STRICT_CONTRACT_VERSION}:
            return value
    return PROJECT_META_DEFAULT_CONTRACT_VERSION


def _normalize_automation_policy(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


class ProjectRegistry:
    """Resolve project ids, directories, and project metadata."""

    def __init__(
        self,
        projects_root: Path,
        *,
        workspace_path: Path | None = None,
        legacy_index_path: Path | None = None,
    ) -> None:
        self.projects_root = projects_root.expanduser().resolve()
        self.workspace_path = workspace_path or resolve_project_workspace_path()
        self.legacy_index_path = legacy_index_path or resolve_project_dir_index_path()
        self._project_dirs: dict[str, Path] = {}
        self._project_display_names: dict[str, str] = {}
        self._hidden_project_ids: set[str] = set()
        self._dirty_project_ids: set[str] = set()
        self._removed_project_ids: set[str] = set()
        self._unhidden_project_ids: set[str] = set()
        self._known_project_roots: set[Path] = {self.projects_root}
        self._load_workspace_file()
        self._load_legacy_project_dir_index()
        self.register_projects_under_root(self.projects_root, persist=False)

    @property
    def project_dirs(self) -> dict[str, Path]:
        return self._project_dirs

    @property
    def known_project_roots(self) -> set[Path]:
        return self._known_project_roots

    def remember_projects_root(self, root: Path) -> Path:
        normalized = root.expanduser().resolve()
        self._known_project_roots.add(normalized)
        return normalized

    def _load_workspace_file(self) -> None:
        if not self.workspace_path.is_file():
            return
        try:
            payload = json.loads(self.workspace_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load project workspace {}: {}", self.workspace_path, exc)
            return
        projects = payload.get("projects") if isinstance(payload, dict) else None
        if not isinstance(projects, list):
            return
        hidden_projects = payload.get("hidden_projects")
        if isinstance(hidden_projects, list):
            for raw_id in hidden_projects:
                if not isinstance(raw_id, str):
                    continue
                try:
                    self._hidden_project_ids.add(validate_project_id(raw_id))
                except ValueError:
                    continue
        for item in projects:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            raw_dir = item.get("project_dir") or item.get("path")
            if not isinstance(raw_id, str) or not isinstance(raw_dir, str):
                continue
            try:
                project_id = validate_project_id(raw_id)
            except ValueError:
                continue
            project_dir = Path(raw_dir).expanduser().resolve()
            if not project_dir.is_dir():
                continue
            self._project_dirs[project_id] = project_dir
            display_name = item.get("display_name") or item.get("name")
            if isinstance(display_name, str) and display_name.strip():
                self._project_display_names[project_id] = display_name.strip()
            self.remember_projects_root(project_dir.parent)

    def _load_legacy_project_dir_index(self) -> None:
        if not self.legacy_index_path.is_file():
            return
        try:
            payload = json.loads(self.legacy_index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load legacy project index {}: {}", self.legacy_index_path, exc)
            return
        if not isinstance(payload, dict):
            return
        changed = False
        for raw_id, raw_path in payload.items():
            if not isinstance(raw_id, str) or not isinstance(raw_path, str):
                continue
            try:
                project_id = validate_project_id(raw_id)
            except ValueError:
                continue
            if project_id in self._project_dirs:
                continue
            project_dir = Path(raw_path).expanduser().resolve()
            if not project_dir.is_dir():
                continue
            self._project_dirs[project_id] = project_dir
            self._dirty_project_ids.add(project_id)
            self.remember_projects_root(project_dir.parent)
            changed = True
        if changed:
            self.save()

    def save(self) -> None:
        merged_dirs: dict[str, Path] = {}
        merged_hidden: set[str] = set()

        def merge_workspace(current_text: str) -> str:
            nonlocal merged_dirs, merged_hidden
            try:
                current = json.loads(current_text) if current_text.strip() else {}
            except json.JSONDecodeError:
                current = {}

            current_projects = current.get("projects") if isinstance(current, dict) else None
            if isinstance(current_projects, list):
                for item in current_projects:
                    if not isinstance(item, dict):
                        continue
                    raw_id = item.get("id")
                    raw_dir = item.get("project_dir") or item.get("path")
                    if not isinstance(raw_id, str) or not isinstance(raw_dir, str):
                        continue
                    try:
                        project_id = validate_project_id(raw_id)
                    except ValueError:
                        continue
                    project_dir = Path(raw_dir).expanduser().resolve()
                    if project_dir.is_dir():
                        merged_dirs[project_id] = project_dir

            for project_id in self._dirty_project_ids:
                project_dir = self._project_dirs.get(project_id)
                if project_dir is None:
                    continue
                existing = merged_dirs.get(project_id)
                project_dir_resolved = project_dir.resolve(strict=False)
                current_root_candidate = (
                    self.projects_root / project_id
                ).resolve(strict=False)
                if (
                    existing is not None
                    and existing.resolve(strict=False) != project_dir_resolved
                    and current_root_candidate != project_dir_resolved
                ):
                    raise ValueError(
                        f"project_id {project_id!r} is already bound to {existing}"
                    )
                if project_dir.is_dir():
                    merged_dirs[project_id] = project_dir
            for project_id in self._removed_project_ids:
                merged_dirs.pop(project_id, None)

            current_hidden = current.get("hidden_projects") if isinstance(current, dict) else None
            if isinstance(current_hidden, list):
                merged_hidden.update(item for item in current_hidden if isinstance(item, str))
            merged_hidden.update(self._hidden_project_ids)
            merged_hidden.difference_update(self._unhidden_project_ids)

            projects: list[dict[str, Any]] = []
            for project_id, project_dir in sorted(merged_dirs.items()):
                meta = self.ensure_project_meta(project_id, project_dir)
                projects.append({
                    "id": project_id,
                    "display_name": str(meta.get("display_name") or project_id),
                    "project_dir": str(project_dir),
                    "created_at": meta.get("created_at"),
                    "updated_at": meta.get("updated_at"),
                })
            payload = {
                "schema_version": 1,
                "default_project_parent": str(self.projects_root),
                "projects": projects,
                "hidden_projects": sorted(merged_hidden),
            }
            return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        try:
            self.workspace_path.parent.mkdir(parents=True, exist_ok=True)
            locked_update_text(self.workspace_path, merge_workspace)
            self._project_dirs = merged_dirs
            self._hidden_project_ids = merged_hidden
        except OSError as exc:
            logger.warning("Failed to persist project workspace {}: {}", self.workspace_path, exc)

    def save_legacy_index(self) -> None:
        def merge_legacy_index(current_text: str) -> str:
            try:
                current = json.loads(current_text) if current_text.strip() else {}
            except json.JSONDecodeError:
                current = {}
            payload = {
                project_id: raw_path
                for project_id, raw_path in current.items()
                if (
                    isinstance(project_id, str)
                    and isinstance(raw_path, str)
                    and Path(raw_path).expanduser().is_dir()
                )
            } if isinstance(current, dict) else {}
            payload.update({
                project_id: str(project_dir)
                for project_id, project_dir in self._project_dirs.items()
                if project_dir.is_dir()
            })
            for project_id in self._removed_project_ids:
                payload.pop(project_id, None)
            return json.dumps(dict(sorted(payload.items())), ensure_ascii=False, indent=2) + "\n"

        try:
            self.legacy_index_path.parent.mkdir(parents=True, exist_ok=True)
            locked_update_text(self.legacy_index_path, merge_legacy_index)
            self._dirty_project_ids.clear()
            self._removed_project_ids.clear()
            self._unhidden_project_ids.clear()
        except OSError as exc:
            logger.warning("Failed to persist legacy project index {}: {}", self.legacy_index_path, exc)

    def project_meta_path(self, project_dir: Path) -> Path:
        return project_dir / PROJECT_META_DIRNAME / PROJECT_META_FILENAME

    def load_project_meta(self, project_dir: Path) -> dict[str, Any]:
        meta_path = self.project_meta_path(project_dir)
        if not meta_path.is_file():
            return {}
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def default_project_meta(
        self,
        project_id: str,
        project_dir: Path,
        *,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        now = now_utc_iso()
        return {
            "id": project_id,
            "project_dir": str(project_dir.expanduser().resolve()),
            "display_name": display_name or project_id,
            "run_mode": PROJECT_META_DEFAULT_RUN_MODE,
            "agent_profile": PROJECT_META_DEFAULT_AGENT_PROFILE,
            "contract_version": PROJECT_META_DEFAULT_CONTRACT_VERSION,
            "automation_policy": None,
            "created_at": now,
            "updated_at": now,
            "schema_version": PROJECT_META_SCHEMA_VERSION,
        }

    def write_project_meta(self, project_dir: Path, meta: dict[str, Any]) -> None:
        meta_path = self.project_meta_path(project_dir)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        locked_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))

    def project_id_for_dir(self, project_dir: Path) -> str | None:
        normalized = project_dir.expanduser().resolve()
        for project_id, registered_dir in self._project_dirs.items():
            if registered_dir.expanduser().resolve(strict=False) == normalized:
                return project_id
        return None

    def ensure_project_meta(
        self,
        project_id: str,
        project_dir: Path,
        *,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        project_id = validate_project_id(project_id)
        project_dir = project_dir.expanduser().resolve()
        current = self.load_project_meta(project_dir)
        preferred_name = (
            display_name
            or self._project_display_names.get(project_id)
            or current.get("display_name")
            or project_id
        )
        baseline = self.default_project_meta(
            project_id,
            project_dir,
            display_name=str(preferred_name).strip() or project_id,
        )
        meta = {**baseline, **current}

        meta["id"] = project_id
        meta["project_dir"] = str(project_dir)
        display = display_name or meta.get("display_name") or project_id
        meta["display_name"] = str(display).strip() or project_id
        meta["run_mode"] = _normalize_run_mode(meta.get("run_mode"))
        meta["agent_profile"] = _normalize_agent_profile(meta.get("agent_profile"))
        meta["contract_version"] = _normalize_contract_version(meta.get("contract_version"))
        meta["automation_policy"] = _normalize_automation_policy(meta.get("automation_policy"))

        if not isinstance(meta.get("schema_version"), int):
            meta["schema_version"] = PROJECT_META_SCHEMA_VERSION
        if not isinstance(meta.get("created_at"), str) or not meta["created_at"]:
            meta["created_at"] = baseline["created_at"]
        if not isinstance(meta.get("updated_at"), str) or not meta["updated_at"]:
            meta["updated_at"] = baseline["updated_at"]

        self._project_display_names[project_id] = str(meta["display_name"])
        if current != meta:
            self.write_project_meta(project_dir, meta)
        return meta

    def is_registered_project_dir(self, project_dir: Path) -> bool:
        return self.project_id_for_dir(project_dir) is not None

    def is_legacy_project_dir(self, project_dir: Path) -> bool:
        return project_dir.is_dir() and project_dir.name.startswith(PROJECT_DIR_PREFIX)

    def register_project_dir(
        self,
        project_id: str,
        project_dir: Path,
        *,
        display_name: str | None = None,
        persist: bool = True,
    ) -> ProjectRef:
        normalized_id = validate_project_id(project_id)
        normalized_dir = project_dir.expanduser().resolve()
        existing = self._project_dirs.get(normalized_id)
        if existing is not None and existing.resolve(strict=False) != normalized_dir:
            raise ValueError(f"project_id {normalized_id!r} is already bound to {existing}")
        self._project_dirs[normalized_id] = normalized_dir
        self._dirty_project_ids.add(normalized_id)
        self._hidden_project_ids.discard(normalized_id)
        self._removed_project_ids.discard(normalized_id)
        self._unhidden_project_ids.add(normalized_id)
        self.remember_projects_root(normalized_dir.parent)
        meta = self.ensure_project_meta(
            normalized_id,
            normalized_dir,
            display_name=display_name,
        )
        if persist:
            self.save()
            self.save_legacy_index()
        return ProjectRef(normalized_id, normalized_dir, meta)

    def drop_project_dir_registration(
        self,
        project_id: str,
        *,
        persist: bool = True,
        hide: bool = False,
    ) -> None:
        normalized_id = validate_project_id(project_id)
        changed = self._project_dirs.pop(normalized_id, None) is not None
        if changed:
            self._removed_project_ids.add(normalized_id)
        if hide and normalized_id not in self._hidden_project_ids:
            self._hidden_project_ids.add(normalized_id)
            self._unhidden_project_ids.discard(normalized_id)
            changed = True
        elif not hide and normalized_id in self._hidden_project_ids:
            self._hidden_project_ids.discard(normalized_id)
            self._unhidden_project_ids.add(normalized_id)
            changed = True
        if changed and persist:
            self.save()
            self.save_legacy_index()

    def register_projects_under_root(self, root: Path, *, persist: bool = True) -> None:
        normalized_root = self.remember_projects_root(root)
        if not normalized_root.is_dir():
            return
        changed = False
        for candidate in normalized_root.iterdir():
            if not self.is_legacy_project_dir(candidate):
                continue
            project_id = candidate.name
            if project_id in self._hidden_project_ids:
                continue
            if project_id in self._project_dirs:
                continue
            self._project_dirs[project_id] = candidate.expanduser().resolve()
            self._dirty_project_ids.add(project_id)
            changed = True
        if changed and persist:
            self.save()
            self.save_legacy_index()

    def resolve(
        self,
        project_id: str,
        *,
        project_dir: Path | str | None = None,
        create: bool = False,
        display_name: str | None = None,
    ) -> ProjectRef:
        normalized_id = validate_project_id(project_id)

        if project_dir is not None:
            resolved_dir = Path(project_dir).expanduser().resolve()
            if create:
                resolved_dir.mkdir(parents=True, exist_ok=True)
            if not resolved_dir.is_dir():
                raise FileNotFoundError(f"project_dir does not exist: {resolved_dir}")
            return self.register_project_dir(
                normalized_id,
                resolved_dir,
                display_name=display_name,
            )

        current_candidate = (self.projects_root / normalized_id).expanduser().resolve()
        if current_candidate.is_dir():
            existing = self._project_dirs.get(normalized_id)
            if existing is not None and existing.resolve(strict=False) != current_candidate:
                self._project_dirs.pop(normalized_id, None)
            return self.register_project_dir(
                normalized_id,
                current_candidate,
                display_name=display_name,
            )

        cached = self._project_dirs.get(normalized_id)
        if cached is not None:
            if cached.is_dir():
                return self.register_project_dir(
                    normalized_id,
                    cached,
                    display_name=display_name,
                    persist=False,
                )
            self.drop_project_dir_registration(normalized_id)

        for root in self._known_project_roots:
            if root == self.projects_root:
                continue
            candidate = (root / normalized_id).expanduser().resolve()
            if candidate.is_dir():
                return self.register_project_dir(
                    normalized_id,
                    candidate,
                    display_name=display_name,
                )

        if create:
            current_candidate.mkdir(parents=True, exist_ok=True)
            return self.register_project_dir(
                normalized_id,
                current_candidate,
                display_name=display_name,
            )

        raise FileNotFoundError(f"project not found: {normalized_id}")

    def create_project(
        self,
        *,
        project_id: str | None,
        display_name: str | None,
        project_parent_dir: Path | str | None = None,
        project_dir: Path | str | None = None,
    ) -> ProjectRef:
        raw_name = (display_name or project_id or "").strip()
        normalized_id = validate_project_id(project_id or slugify_project_id(raw_name))
        if normalized_id in self._project_dirs and self._project_dirs[normalized_id].is_dir():
            raise FileExistsError(f"project already exists: {normalized_id}")

        if project_dir is not None:
            target_dir = Path(project_dir).expanduser().resolve()
        else:
            parent = Path(project_parent_dir).expanduser() if project_parent_dir else self.projects_root
            target_dir = (parent / normalized_id).expanduser().resolve()

        if target_dir.exists() and not target_dir.is_dir():
            raise FileExistsError(f"project path is not a directory: {target_dir}")
        if target_dir.is_dir() and any(target_dir.iterdir()):
            meta = self.load_project_meta(target_dir)
            if meta.get("id") != normalized_id:
                raise FileExistsError(f"project directory is not empty: {target_dir}")

        target_dir.mkdir(parents=True, exist_ok=True)
        return self.register_project_dir(
            normalized_id,
            target_dir,
            display_name=(display_name or normalized_id).strip() or normalized_id,
        )

    def list_projects(self) -> list[ProjectRef]:
        self.register_projects_under_root(self.projects_root)
        refs: list[ProjectRef] = []
        stale: list[str] = []
        for project_id, project_dir in sorted(self._project_dirs.items()):
            if not project_dir.is_dir():
                stale.append(project_id)
                continue
            refs.append(self.register_project_dir(project_id, project_dir, persist=False))
        for project_id in stale:
            self._project_dirs.pop(project_id, None)
        if stale:
            self.save()
            self.save_legacy_index()
        return refs
