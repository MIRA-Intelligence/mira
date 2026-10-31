import json
from pathlib import Path

import pytest

from mira_engine.organization import OrganizationStore


def test_folder_chat_and_assignment_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "organization.json"
    store = OrganizationStore(path)
    folder = store.create_folder(" Lung cancer ", "chat")
    projects = store.create_folder("Lung cancer", "project")
    store.upsert_chats([
        {
            "id": "chat-1",
            "title": "Initial question",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    ])
    store.assign("chat", "chat-1", folder["id"])
    store.assign("project", "PRJ-1", projects["id"])

    reloaded = OrganizationStore(path).snapshot()
    assert [(f["name"], f["kind"]) for f in reloaded["folders"]] == [
        ("Lung cancer", "chat"),
        ("Lung cancer", "project"),
    ]
    assert reloaded["chats"][0]["title"] == "Initial question"
    assert reloaded["assignments"] == {
        "chat": {"chat-1": folder["id"]},
        "project": {"PRJ-1": projects["id"]},
    }


def test_folder_names_are_case_insensitively_unique(tmp_path: Path) -> None:
    store = OrganizationStore(tmp_path / "organization.json")
    store.create_folder("Glioma", "project")
    with pytest.raises(FileExistsError):
        store.create_folder(" glioma ", "project")
    store.create_folder("glioma", "chat")
    with pytest.raises(FileExistsError):
        store.create_folder("GLIOMA")


def test_deleting_folder_moves_contents_to_uncategorized(tmp_path: Path) -> None:
    store = OrganizationStore(tmp_path / "organization.json")
    folder = store.create_folder("Topic", "chat")
    store.upsert_chats([{"id": "chat-1", "title": "Question"}])
    store.assign("chat", "chat-1", folder["id"])

    store.delete_folder(folder["id"])

    snapshot = store.snapshot()
    assert snapshot["folders"] == []
    assert snapshot["assignments"] == {"chat": {}, "project": {}}
    assert snapshot["chats"][0]["id"] == "chat-1"


def test_chat_import_is_idempotent_and_delete_cleans_assignment(tmp_path: Path) -> None:
    store = OrganizationStore(tmp_path / "organization.json")
    folder = store.create_folder("Topic")
    payload = [{"id": "chat-1", "title": "Question", "updated_at": "2026-01-01T00:00:00Z"}]
    store.upsert_chats(payload)
    store.upsert_chats(payload)
    store.assign("chat", "chat-1", folder["id"])
    assert len(store.snapshot()["chats"]) == 1

    store.delete_chat("chat-1")

    assert store.snapshot()["chats"] == []
    assert store.snapshot()["assignments"]["chat"] == {}


def test_stale_chat_cache_does_not_overwrite_newer_backend_title(tmp_path: Path) -> None:
    store = OrganizationStore(tmp_path / "organization.json")
    store.upsert_chats([{
        "id": "chat-1",
        "title": "New title",
        "updated_at": "2026-02-01T00:00:00Z",
    }])

    store.upsert_chats([{
        "id": "chat-1",
        "title": "Old cached title",
        "updated_at": "2026-01-01T00:00:00Z",
    }])

    assert store.snapshot()["chats"][0]["title"] == "New title"


def test_invalid_existing_document_is_safely_normalized(tmp_path: Path) -> None:
    path = tmp_path / "organization.json"
    path.write_text(json.dumps({"folders": [{"id": "bad/id", "name": "x"}]}), encoding="utf-8")

    assert OrganizationStore(path).snapshot() == {
        "schema_version": 1,
        "folders": [],
        "chats": [],
        "assignments": {"chat": {}, "project": {}},
    }


def test_folder_belongs_to_one_list(tmp_path: Path) -> None:
    store = OrganizationStore(tmp_path / "organization.json")
    folder = store.create_folder("Topic", "project")
    store.assign("project", "PRJ-1", folder["id"])
    store.upsert_chats([{"id": "chat-1", "title": "Question"}])

    with pytest.raises(ValueError, match="holds projects"):
        store.assign("chat", "chat-1", folder["id"])
    with pytest.raises(ValueError, match="kind must be"):
        store.create_folder("Other", "workspace")
    assert store.snapshot()["assignments"]["chat"] == {}


def test_unscoped_folder_takes_the_kind_of_its_first_item(tmp_path: Path) -> None:
    # Older desktop clients create folders without a kind.
    store = OrganizationStore(tmp_path / "organization.json")
    folder = store.create_folder("Topic")
    assert folder["kind"] is None

    store.assign("project", "PRJ-1", folder["id"])

    assert store.snapshot()["folders"][0]["kind"] == "project"
    with pytest.raises(ValueError):
        store.assign("chat", "chat-1", folder["id"])


def test_legacy_shared_folders_are_scoped_on_load(tmp_path: Path) -> None:
    path = tmp_path / "organization.json"
    path.write_text(json.dumps({
        "folders": [
            {"id": "f-chat", "name": "Chats only"},
            {"id": "f-proj", "name": "Projects only"},
            {"id": "f-both", "name": "Both"},
            {"id": "f-empty", "name": "Empty"},
        ],
        "assignments": {
            "chat": {"chat-1": "f-chat", "chat-2": "f-both"},
            "project": {"PRJ-1": "f-proj", "PRJ-2": "f-both"},
        },
    }), encoding="utf-8")

    snapshot = OrganizationStore(path).snapshot()

    kinds = {f["id"]: f["kind"] for f in snapshot["folders"]}
    assert kinds["f-chat"] == "chat"
    assert kinds["f-proj"] == "project"
    assert kinds["f-both"] == "project"
    assert kinds["f-empty"] is None
    twin = next(f for f in snapshot["folders"] if f["name"] == "Both" and f["kind"] == "chat")
    assert snapshot["assignments"]["project"] == {"PRJ-1": "f-proj", "PRJ-2": "f-both"}
    assert snapshot["assignments"]["chat"] == {"chat-1": "f-chat", "chat-2": twin["id"]}
