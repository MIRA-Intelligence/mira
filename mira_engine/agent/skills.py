"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

from mira_engine.agent.skill_plugins import SkillPluginError, SkillPluginManager

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = BUILTIN_SKILLS_DIR,
        plugin_manager: SkillPluginManager | None = None,
    ):
        self.workspace = workspace
        from mira_engine.utils.helpers import get_mira_dir

        # Backward compatibility:
        # - legacy tests/projects place skills under "<workspace>/skills"
        # - runtime state stores skills under "<workspace>/.mira/skills"
        # Search both, preferring direct workspace path.
        direct_skills = workspace / "skills"
        mira_skills = get_mira_dir(workspace) / "skills"
        roots: list[Path] = []
        for root in (direct_skills, mira_skills):
            if all(existing != root for existing in roots):
                roots.append(root)
        self.workspace_skills_roots = roots
        # Preserve old attribute name for compatibility with existing code.
        self.workspace_skills = roots[0]
        # None explicitly disables builtin skills.
        self.builtin_skills = builtin_skills_dir
        # Test/compat behavior: when caller injects a custom builtin dir, avoid
        # auto-discovering global/plugin skills unless explicitly requested.
        if plugin_manager is not None:
            self.plugin_manager = plugin_manager
        elif builtin_skills_dir is BUILTIN_SKILLS_DIR:
            self.plugin_manager = SkillPluginManager(workspace)
        else:
            self.plugin_manager = None

    def _list_plugin_skills(self) -> list[dict[str, str]]:
        if self.plugin_manager is None:
            return []
        try:
            return self.plugin_manager.list_enabled_skills()
        except SkillPluginError:
            return []

    def _managed_skill_names(self) -> set[str]:
        if self.plugin_manager is None:
            return set()
        try:
            return self.plugin_manager.get_managed_skill_names()
        except SkillPluginError:
            return set()

    def _plugin_skill_path_by_name(self, name: str) -> str | None:
        for entry in self._list_plugin_skills():
            if entry.get("name") == name:
                return entry.get("path")
        return None

    def _builtin_skill_path_by_name(self, name: str) -> Path | None:
        if not self.builtin_skills or not self.builtin_skills.exists():
            return None
        for skill_file in self.builtin_skills.rglob("SKILL.md"):
            if skill_file.parent.name == name:
                return skill_file
        return None

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []

        # Workspace skills (highest priority)
        seen_names: set[str] = set()
        for root in self.workspace_skills_roots:
            if not root.exists():
                continue
            for skill_file in root.rglob("SKILL.md"):
                if not skill_file.is_file():
                    continue
                skill_name = skill_file.parent.name
                if skill_name in seen_names:
                    continue
                seen_names.add(skill_name)
                skills.append({"name": skill_name, "path": str(skill_file), "source": "workspace"})

        # Plugin skills (global install + scope toggles)
        for plugin_skill in self._list_plugin_skills():
            name = plugin_skill.get("name")
            path = plugin_skill.get("path")
            if not isinstance(name, str) or not isinstance(path, str):
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            skills.append({
                "name": name,
                "path": path,
                "source": "plugin",
            })

        # Built-in skills
        managed_names = self._managed_skill_names()
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_file in self.builtin_skills.rglob("SKILL.md"):
                if not skill_file.is_file():
                    continue
                skill_name = skill_file.parent.name
                if skill_name in seen_names or skill_name in managed_names:
                    continue
                seen_names.add(skill_name)
                skills.append({"name": skill_name, "path": str(skill_file), "source": "builtin"})

        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # Check workspace roots first
        for root in self.workspace_skills_roots:
            workspace_skill = root / name / "SKILL.md"
            if workspace_skill.exists():
                return workspace_skill.read_text(encoding="utf-8")

        plugin_path = self._plugin_skill_path_by_name(name)
        if plugin_path:
            plugin_skill = Path(plugin_path)
            if plugin_skill.is_file():
                return plugin_skill.read_text(encoding="utf-8")

        if name in self._managed_skill_names():
            return None

        # Check built-in
        builtin_skill = self._builtin_skill_path_by_name(name)
        if builtin_skill:
            return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_mira_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter (supports mira and openclaw keys)."""
        try:
            data = json.loads(raw)
            return data.get("mira", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get mira metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_mira_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_mira_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None

    def suggest_skills(
        self,
        query: str,
        *,
        recent: list[str] | None = None,
        limit: int = 3,
    ) -> list[str]:
        """Suggest likely relevant skills for a user query."""
        text = (query or "").strip()
        if not text:
            return []
        if limit < 1:
            limit = 1

        available = self.list_skills(filter_unavailable=True)
        if not available:
            return []

        recent_names = [name for name in (recent or []) if isinstance(name, str)]
        is_follow_up = self._looks_like_follow_up(text)
        if is_follow_up and recent_names:
            picked: list[str] = []
            for name in recent_names:
                if any(s["name"] == name for s in available) and name not in picked:
                    picked.append(name)
                if len(picked) >= limit:
                    break
            if picked:
                return picked

        query_tokens = self._tokenize(text)
        query_lc = text.lower()
        scored: list[tuple[int, str]] = []

        for entry in available:
            name = entry["name"]
            meta = self.get_skill_metadata(name) or {}
            desc = str(meta.get("description") or "")
            path = str(entry.get("path") or "")

            base_text = " ".join((name, desc, path))
            score = self._score_skill_match(
                query_lc=query_lc,
                query_tokens=query_tokens,
                skill_name=name,
                skill_text=base_text,
            )
            if score > 0:
                scored.append((score, name))

        scored.sort(key=lambda item: (-item[0], item[1]))
        result: list[str] = []
        for _, name in scored:
            if name not in result:
                result.append(name)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _looks_like_follow_up(text: str) -> bool:
        lowered = text.lower()
        markers = (
            "继续", "接着", "刚才", "之前", "上次", "继续之前", "continue", "resume", "previous", "last task",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        lowered = text.lower()
        latin = re.findall(r"[a-z0-9][a-z0-9._+-]*", lowered)
        cjk_chunks = re.findall(r"[\u4e00-\u9fff]+", text)
        tokens: set[str] = set(latin)
        for chunk in cjk_chunks:
            tokens.add(chunk)
            if len(chunk) > 1:
                tokens.update(chunk)
        return {t for t in tokens if t}

    @staticmethod
    def _skill_aliases(skill_name: str) -> tuple[str, ...]:
        aliases: dict[str, tuple[str, ...]] = {
            "medical-image-dl-pipeline": (
                "medical", "imaging", "mri", "ct", "dicom", "nifti", "monai", "artifact", "motion",
                "ghost", "k-space", "2.5d", "3d", "unet", "radiology", "医学", "影像", "伪影", "呼吸", "运动",
                "去伪影",
            ),
            "dicom2nifti": ("dicom", "nifti", "医学", "影像", "转换"),
            "monai": ("monai", "medical", "imaging", "医学", "影像"),
        }
        return aliases.get(skill_name, ())

    def _score_skill_match(
        self,
        *,
        query_lc: str,
        query_tokens: set[str],
        skill_name: str,
        skill_text: str,
    ) -> int:
        score = 0
        skill_lc = skill_text.lower()
        name_tokens = self._tokenize(skill_name.replace("-", " "))
        score += len(name_tokens.intersection(query_tokens)) * 4

        skill_tokens = self._tokenize(skill_text)
        score += len(skill_tokens.intersection(query_tokens)) * 2

        for alias in self._skill_aliases(skill_name):
            alias_lc = alias.lower()
            if alias_lc in query_lc:
                score += 6
            if alias_lc in skill_lc:
                score += 1
        return score
