"""Workspace-level folders and quick-chat catalog for the desktop UI."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mira_engine.utils.locks import locked_write_text

ORGANIZATION_FILENAME = "organization.json"
ORGANIZATION_SCHEMA_VERSION = 1
FOLDER_NAME_MAX_CHARS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_item_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("item id must be a string")
    item_id = value.strip()
    if not item_id or len(item_id) > 128 or item_id in {".", ".."}:
        raise ValueError("item id must be 1-128 characters")
    if any(ch in item_id for ch in {"/", "\\", ":", "\x00"}) or any(ord(ch) < 32 for ch in item_id):
        raise ValueError("item id contains invalid characters")
    return item_id


def _valid_folder_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("folder name must be a string")
    name = value.strip()
    if not name:
        raise ValueError("folder name is required")
    if len(name) > FOLDER_NAME_MAX_CHARS:
        raise ValueError(f"folder name must be at most {FOLDER_NAME_MAX_CHARS} characters")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("folder name contains control characters")
    return name


class OrganizationStore:
    """Small, atomically persisted organization document scoped to one workspace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = self._load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": ORGANIZATION_SCHEMA_VERSION,
            "folders": [],
            "chats": [],
            "assignments": {"chat": {}, "project": {}},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._empty()
        if not isinstance(raw, dict):
            return self._empty()

        data = self._empty()
        folder_ids: set[str] = set()
        names: set[str] = set()
        for item in raw.get("folders", []):
            if not isinstance(item, dict):
                continue
            try:
                folder_id = _valid_item_id(item.get("id"))
                name = _valid_folder_name(item.get("name"))
            except ValueError:
                continue
            folded = name.casefold()
            if folder_id in folder_ids or folded in names:
                continue
            folder_ids.add(folder_id)
            names.add(folded)
            data["folders"].append({
                "id": folder_id,
                "name": name,
                "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else _now(),
                "updated_at": item.get("updated_at") if isinstance(item.get("updated_at"), str) else _now(),
            })

        chat_ids: set[str] = set()
        for item in raw.get("chats", []):
            if not isinstance(item, dict):
                continue
            try:
                chat_id = _valid_item_id(item.get("id"))
            except ValueError:
                continue
            if chat_id in chat_ids:
                continue
            chat_ids.add(chat_id)
            data["chats"].append({
                "id": chat_id,
                "title": str(item.get("title") or "")[:60],
                "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else _now(),
                "updated_at": item.get("updated_at") if isinstance(item.get("updated_at"), str) else _now(),
            })

        raw_assignments = raw.get("assignments")
        if isinstance(raw_assignments, dict):
            for kind in ("chat", "project"):
                mapping = raw_assignments.get(kind)
                if not isinstance(mapping, dict):
                    continue
                for raw_item_id, raw_folder_id in mapping.items():
                    try:
                        item_id = _valid_item_id(raw_item_id)
                        folder_id = _valid_item_id(raw_folder_id)
                    except ValueError:
                        continue
                    if folder_id in folder_ids:
                        data["assignments"][kind][item_id] = folder_id
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        locked_write_text(
            self.path,
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
        )

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def _folder(self, folder_id: str) -> dict[str, Any] | None:
        return next((item for item in self._data["folders"] if item["id"] == folder_id), None)

    def _assert_unique_name(self, name: str, *, excluding: str | None = None) -> None:
        folded = name.casefold()
        if any(item["id"] != excluding and item["name"].casefold() == folded for item in self._data["folders"]):
            raise FileExistsError("a folder with this name already exists")

    def create_folder(self, name: Any) -> dict[str, Any]:
        normalized = _valid_folder_name(name)
        self._assert_unique_name(normalized)
        now = _now()
        folder = {"id": f"folder-{uuid.uuid4().hex}", "name": normalized, "created_at": now, "updated_at": now}
        self._data["folders"].append(folder)
        self._save()
        return dict(folder)

    def rename_folder(self, folder_id: str, name: Any) -> dict[str, Any]:
        folder_id = _valid_item_id(folder_id)
        folder = self._folder(folder_id)
        if folder is None:
            raise KeyError(folder_id)
        normalized = _valid_folder_name(name)
        self._assert_unique_name(normalized, excluding=folder_id)
        folder["name"] = normalized
        folder["updated_at"] = _now()
        self._save()
        return dict(folder)

    def delete_folder(self, folder_id: str) -> None:
        folder_id = _valid_item_id(folder_id)
        if self._folder(folder_id) is None:
            raise KeyError(folder_id)
        self._data["folders"] = [item for item in self._data["folders"] if item["id"] != folder_id]
        for mapping in self._data["assignments"].values():
            for item_id in [key for key, value in mapping.items() if value == folder_id]:
                mapping.pop(item_id, None)
        self._save()

    def assign(self, kind: str, item_id: str, folder_id: Any) -> None:
        if kind not in {"chat", "project"}:
            raise ValueError("kind must be chat or project")
        item_id = _valid_item_id(item_id)
        mapping = self._data["assignments"][kind]
        if folder_id is None:
            mapping.pop(item_id, None)
        else:
            normalized_folder_id = _valid_item_id(folder_id)
            if self._folder(normalized_folder_id) is None:
                raise KeyError(normalized_folder_id)
            mapping[item_id] = normalized_folder_id
        self._save()

    def upsert_chats(self, chats: Any) -> list[dict[str, Any]]:
        if not isinstance(chats, list):
            raise ValueError("chats must be an array")
        by_id = {item["id"]: item for item in self._data["chats"]}
        now = _now()
        for raw in chats:
            if not isinstance(raw, dict):
                raise ValueError("each chat must be an object")
            chat_id = _valid_item_id(raw.get("id"))
            title = str(raw.get("title") or "").strip()[:60]
            existing = by_id.get(chat_id)
            if existing is None:
                existing = {
                    "id": chat_id,
                    "title": title,
                    "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else now,
                    "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else now,
                }
                self._data["chats"].append(existing)
                by_id[chat_id] = existing
            else:
                raw_updated = raw.get("updated_at")
                incoming_updated = raw_updated if isinstance(raw_updated, str) else ""
                if title and (not existing["title"] or incoming_updated > existing["updated_at"]):
                    existing["title"] = title
                if incoming_updated:
                    existing["updated_at"] = max(existing["updated_at"], incoming_updated)
        self._save()
        return [dict(item) for item in self._data["chats"]]

    def update_chat(self, chat_id: str, *, title: Any = None, touch: bool = False) -> dict[str, Any]:
        chat_id = _valid_item_id(chat_id)
        chat = next((item for item in self._data["chats"] if item["id"] == chat_id), None)
        if chat is None:
            raise KeyError(chat_id)
        if title is not None:
            if not isinstance(title, str):
                raise ValueError("title must be a string")
            normalized = title.strip()
            if normalized:
                chat["title"] = normalized[:60]
        if touch or title is not None:
            chat["updated_at"] = _now()
        self._save()
        return dict(chat)

    def delete_chat(self, chat_id: str) -> None:
        chat_id = _valid_item_id(chat_id)
        before = len(self._data["chats"])
        self._data["chats"] = [item for item in self._data["chats"] if item["id"] != chat_id]
        if len(self._data["chats"]) == before:
            raise KeyError(chat_id)
        self._data["assignments"]["chat"].pop(chat_id, None)
        self._save()

    def remove_project(self, project_id: str) -> None:
        project_id = _valid_item_id(project_id)
        if self._data["assignments"]["project"].pop(project_id, None) is not None:
            self._save()
