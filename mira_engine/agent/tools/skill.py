"""Load a skill's full instructions into the conversation on demand.

The system prompt only lists skills (name + one-line description). Calling
this tool returns the SKILL.md body, prefixed with the skill's base directory
so bundled scripts and references can be read from there.
"""

import difflib
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from mira_engine.agent.skills import SKILL_TOOL_NAME, SkillsLoader
from mira_engine.agent.tools.base import UNSET, Tool, turn_value


class SkillTool(Tool):
    """Return a skill's expanded instructions."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._project_dir: Path | None = None
        self._runtime_project_dir: ContextVar[Path | None] = ContextVar(
            "skill_project_dir",
            default=UNSET,
        )
        self._loaders: dict[Path, SkillsLoader] = {}

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        self._project_dir = Path(project_dir)
        self._runtime_project_dir.set(Path(project_dir))

    def clear_project_context(self) -> None:
        self._project_dir = None
        self._runtime_project_dir.set(None)

    def _loader(self) -> SkillsLoader:
        workspace = turn_value(self._runtime_project_dir, self._project_dir) or self._workspace
        loader = self._loaders.get(workspace)
        if loader is None:
            loader = self._loaders[workspace] = SkillsLoader(workspace)
        return loader

    @property
    def name(self) -> str:
        return SKILL_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Load a skill's full instructions into the conversation. Skills are listed by name "
            "and description in the system prompt. When a skill matches the user's request, call "
            "this tool BEFORE doing the task, then follow the returned instructions. Do not call "
            "it again for a skill whose instructions are already in this conversation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The skill name from the skills list, e.g. 'docx' or 'latex-posters'.",
                },
                "args": {
                    "type": "string",
                    "description": "Optional arguments for the skill.",
                },
            },
            "required": ["skill"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, skill: str, args: str | None = None, **kwargs: Any) -> str:
        name = (skill or "").strip().lstrip("/")
        loader = self._loader()
        if not loader.is_model_invocable(name):
            return f"Error: skill '{name}' can only be selected by the user, not invoked by the model."
        content = loader.render_skill(name, args)
        if content is None:
            names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
            close = difflib.get_close_matches(name, names, n=5, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return f"Error: unknown skill '{name}'.{hint}"
        return content
