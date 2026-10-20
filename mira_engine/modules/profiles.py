"""Composable agent-profile policies for native ModuleRun executors."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

AgentProfile = Literal["research", "engineer", "team"]

_PROFILE_FILES: dict[AgentProfile, str] = {
    "research": "AGENTS_RS.md",
    "engineer": "AGENTS_EG.md",
    "team": "AGENTS_TM.md",
}

# These sections implement the legacy linear ResearchAgentLoop workflow or
# out-of-band scheduling. Native executors replace them with ModuleRun
# interactions, checkpoints, completion, and explicit handoffs.
_LEGACY_SECTION_PREFIXES = (
    "interactive plan mode",
    "scheduled reminders",
)


def normalize_agent_profile(value: object) -> AgentProfile:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _PROFILE_FILES:
            return normalized  # type: ignore[return-value]
    return "research"


def _without_legacy_sections(markdown: str) -> str:
    kept: list[str] = []
    skip = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            skip = heading.startswith(_LEGACY_SECTION_PREFIXES)
        if not skip:
            kept.append(line)
    return "\n".join(kept).strip()


@lru_cache(maxsize=3)
def profile_system_instructions(profile: AgentProfile) -> str:
    templates_dir = Path(__file__).resolve().parents[1] / "templates"
    source = (templates_dir / _PROFILE_FILES[profile]).read_text(encoding="utf-8")
    policy = _without_legacy_sections(source)
    return (
        f"Active agent profile: {profile}.\n"
        "Apply the following profile as scientific/engineering/team policy inside "
        "this ModuleRun only. Its rigor, review, audit-record, reproducibility, and "
        "delegation rules remain mandatory. Any legacy cross-module ordering, "
        "set_plan/task_plan workflow, scheduled-job instruction, or instruction to "
        "start another stage is superseded by the native executor contract. Treat "
        "cycle steps outside the current module as review criteria or suggested "
        "handoffs, never as permission to execute another module. Persist profile "
        "record formats as Run-local files/Artifacts instead of shared MEMORY.md or "
        "task_plan.json. Perform Git operations only when the current executor "
        "capabilities and user-approved scope permit them.\n\n"
        f"{policy}"
    )


def profile_allows_team_tools(profile: AgentProfile) -> bool:
    return profile == "team"
