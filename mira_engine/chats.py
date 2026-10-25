"""Working directories for UI Quick Chat conversations.

Each chat gets ``<workspace>/chats/<chat-id>/``, next to ``<workspace>/projects/``.
It is the default ``exec`` working directory for the chat's turns and the folder
the UI resource explorer opens for it.

Before this layout, the prompt told the model "Project Directory:
projects/<chat-id>" without saying what it was relative to, so files ended up
in ``<workspace>/.mira/projects/<chat-id>`` or ``<workspace>/projects/<chat-id>``.
A chat that already has such a folder keeps using it.
"""

from __future__ import annotations

from pathlib import Path

from mira_engine.projects import validate_project_id

CHATS_DIRNAME = "chats"
_LEGACY_CHAT_PARENTS = (Path(".mira") / "projects", Path("projects"))


def chat_workdir(workspace: Path, chat_id: str) -> Path:
    """Return the chat's working directory without creating it.

    Raises ``ValueError`` for ids that cannot be a single path component.
    """
    chat_id = validate_project_id(chat_id)
    root = workspace.expanduser().resolve()
    current = root / CHATS_DIRNAME / chat_id
    if current.is_dir():
        return current
    for parent in _LEGACY_CHAT_PARENTS:
        legacy = root / parent / chat_id
        if legacy.is_dir():
            return legacy
    return current


def ensure_chat_workdir(workspace: Path, chat_id: str) -> Path:
    """Return the chat's working directory, creating it if needed."""
    path = chat_workdir(workspace, chat_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
